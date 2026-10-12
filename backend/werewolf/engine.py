"""Authoritative six-seat rules and filtered player views.

Only the trusted coordinator may use next_actor(), snapshot(), or from_snapshot().
Players and future model adapters receive player_view() and submit Action objects.
"""

from __future__ import annotations

import random
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, ClassVar


class RuleViolation(ValueError):
    """An action or configuration is invalid; no action was applied."""


@dataclass(frozen=True)
class GameConfig:
    version: str = "six-player-v3-witch"
    players: int = 6
    wolves: int = 2
    seers: int = 1
    villagers: int = 2
    witches: int = 1
    hunters: int = 0
    guards: int = 0
    knights: int = 0
    speech_limit: int = 300
    wolf_message_limit: int = 150
    last_words: bool = False
    win_condition: str = "eliminate_side"

    def __post_init__(self) -> None:
        if type(self.last_words) is not bool:
            raise RuleViolation("遗言开关必须为布尔值")
        if type(self.win_condition) is not str or self.win_condition not in {"eliminate_side", "parity"}:
            raise RuleViolation("不支持的胜负规则")
        if any(type(v) is not int for v in (self.players, self.wolves, self.seers, self.witches,
                                            self.hunters, self.guards, self.knights,
                                            self.villagers, self.speech_limit, self.wolf_message_limit)):
            raise RuleViolation("人数、角色数量和字数限制必须为整数")
        expected = {"version": "six-player-v3-witch", "players": 6, "wolves": 2,
                    "seers": 1, "villagers": 2, "witches": 1, "speech_limit": 300,
                    "wolf_message_limit": 150, "hunters": 0, "guards": 0, "knights": 0}
        legacy = {**expected, "version": "six-player-v2", "villagers": 3, "witches": 0}
        nine = {**expected, "version": "nine-player-v1", "players": 9, "wolves": 3, "villagers": 4}
        if {k: v for k, v in asdict(self).items() if k not in {"last_words", "win_condition"}} in (expected, legacy, nine):
            return
        if (self.version not in {"custom-player-v1", "custom-player-v2-gods", "nine-player-v2-gods"}
                or any(type(v) is not int for v in (self.players, self.wolves, self.seers, self.witches, self.villagers))
                or not 6 <= self.players <= 9 or not 1 <= self.wolves < self.players - self.wolves
                or any(n not in (0, 1) for n in (self.seers, self.witches, self.hunters, self.guards, self.knights))
                or self.villagers < 1
                or self.wolves + self.seers + self.witches + self.hunters + self.guards + self.knights + self.villagers != self.players
                or self.speech_limit != 300 or self.wolf_message_limit != 150):
            raise RuleViolation("自定义仅支持 6–9 人、至少一狼一村民、狼少于好人，各神职最多一名")
        if self.version != "custom-player-v1" and (self.seers != 1 or self.seers + self.witches + self.hunters + self.guards + self.knights != 3):
            raise RuleViolation("新版阵容必须包含预言家和另外两名不同神职")

    @classmethod
    def legacy(cls) -> GameConfig:
        return cls(version="six-player-v2", villagers=3, witches=0, win_condition="parity")

    def public_rules(self) -> dict[str, Any]:
        rules = asdict(self)
        # Historical views lacked this field; preserve their configuration fingerprints.
        if self.win_condition == "parity":
            rules.pop("win_condition")
        return rules


@dataclass(frozen=True)
class Action:
    kind: str
    target: int | None = None
    text: str | None = None
    potion: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Action:
        if not isinstance(data, dict) or set(data) - {"kind", "target", "text", "potion"}:
            raise RuleViolation("行动只能包含 kind、target、text、potion")
        if "kind" not in data:
            raise RuleViolation("行动缺少 kind")
        return cls(**data)


def validate_action(action: Action, spec: dict[str, Any]) -> None:
    """Shared action validation for the engine and the model response boundary."""
    if not isinstance(action, Action) or action.kind != spec["kind"]:
        raise RuleViolation("当前阶段不允许该行动")
    if action.kind == "witch":
        choices = spec["potions"]
        if not isinstance(action.potion, str) or action.potion not in choices:
            raise RuleViolation("药剂已用尽或本次不允许使用")
        if action.text is not None:
            raise RuleViolation("用药行动不接受发言字段")
        if action.potion == "skip":
            if action.target is not None:
                raise RuleViolation("不用药时目标必须为 null")
        elif type(action.target) is not int or action.target not in choices[action.potion]:
            raise RuleViolation("非法用药目标，不允许自救、自毒或操作出局玩家")
        return
    if action.potion is not None:
        raise RuleViolation("该行动不接受药剂字段")
    if "max_length" in spec:
        if (not isinstance(action.text, str) or (not action.text.strip() and not spec.get("allow_empty", False))
                or len(action.text) > spec["max_length"]):
            raise RuleViolation("发言或提案理由必须非空且在字数限制内")
    elif action.text is not None:
        raise RuleViolation("目标行动不接受发言字段")
    if "targets" in spec:
        if action.target is None:
            if not spec["allow_abstain"]:
                raise RuleViolation("该行动不可弃权")
        elif type(action.target) is not int or action.target not in spec["targets"]:
            raise RuleViolation("非法目标")
    elif action.target is not None:
        raise RuleViolation("该发言行动不接受目标字段")


class GameEngine:
    SNAPSHOT_VERSION = 6
    NIGHT_PHASES = frozenset({"guard", "wolf_propose", "wolf_reply", "wolf_vote", "seer", "witch"})
    PHASE_ACTION: ClassVar[dict[str, str]] = {
        "wolf_propose": "propose_kill", "wolf_reply": "wolf_message",
        "wolf_vote": "kill", "seer": "inspect", "witch": "witch",
        "day_speech": "speak", "vote": "vote", "runoff_speech": "speak",
        "runoff_vote": "vote",
        "guard": "protect", "hunter_shot": "shoot", "knight_duel": "duel",
        "last_words": "last_words",
    }

    def __init__(self, seed: int = 0, config: GameConfig | None = None, *, wolf_reply_mode: str = "batch") -> None:
        if type(seed) is not int:
            raise RuleViolation("随机种子必须为整数")
        self.config = config or GameConfig()
        if wolf_reply_mode not in {"batch", "sequential"}:
            raise RuleViolation("未知狼队回应方式")
        self._wolf_reply_mode = wolf_reply_mode
        self._snapshot_version = self.SNAPSHOT_VERSION
        self._explicit_win_condition = True
        self._rng = random.Random(seed)
        roles = ["wolf"] * self.config.wolves + ["seer"] * self.config.seers \
            + ["witch"] * self.config.witches + ["hunter"] * self.config.hunters \
            + ["guard"] * self.config.guards + ["knight"] * self.config.knights \
            + ["villager"] * self.config.villagers
        self._rng.shuffle(roles)
        self._state: dict[str, Any] = {
            "seed": seed,
            "players": [{"seat": i + 1, "role": role, "alive": True}
                        for i, role in enumerate(roles)],
            "day": 1, "phase": "wolf_propose", "queue": [], "cursor": 0,
            "wolf_votes": {}, "wolf_proposals": {}, "wolf_replies": {},
            "ballots": {}, "pending_kill": None, "pending_save": None, "pending_poison": None,
            "potions": {str(i + 1): {"save": True, "poison": True}
                        for i, role in enumerate(roles) if role == "witch"},
            "tied": [], "checks": {}, "events": [], "winner": None,
            "revision": 0,
            "last_guard": {}, "protected": None, "knight_used": [], "hunter_used": [],
            "after_shot": None, "speech_remaining": [],
            "after_last_words": None, "last_words_shot_candidates": [],
        }
        self._emit("game_started", {"players": self.config.players, "roster": {
            "wolf": self.config.wolves, "seer": self.config.seers,
            "witch": self.config.witches, "villager": self.config.villagers,
            **{r: n for r, n in (("hunter", self.config.hunters), ("guard", self.config.guards), ("knight", self.config.knights)) if n},
        }})
        self._start_night()

    @property
    def finished(self) -> bool:
        return self._state["winner"] is not None

    def next_actor(self) -> int | None:
        """Trusted scheduling interface, never send it unfiltered to a player."""
        queue = self._state["queue"]
        cursor = self._state["cursor"]
        return queue[cursor] if cursor < len(queue) else None

    def _player(self, seat: int) -> dict[str, Any]:
        if type(seat) is not int or not 1 <= seat <= self.config.players:
            raise RuleViolation("无效席位")
        return self._state["players"][seat - 1]

    def _living(self, role: str | None = None) -> list[int]:
        return [p["seat"] for p in self._state["players"]
                if p["alive"] and (role is None or p["role"] == role)]

    def _emit(self, kind: str, data: dict[str, Any], audience: list[int] | None = None) -> None:
        self._state["events"].append({
            "day": self._state["day"], "kind": kind,
            "data": deepcopy(data), "audience": audience,
        })

    def _phase(self, phase: str, queue: list[int]) -> None:
        self._state.update(phase=phase, queue=queue, cursor=0)

    def _start_night(self) -> None:
        self._state.update(wolf_votes={}, wolf_proposals={}, wolf_replies={},
                           ballots={}, pending_kill=None, pending_save=None, pending_poison=None, protected=None, tied=[])
        self._emit("night_started", {})
        guards = self._living("guard")
        if guards:
            self._phase("guard", guards)
        else:
            self._wolves_start()

    def _wolves_start(self) -> None:
        wolves = self._living("wolf")
        self._phase("wolf_vote" if len(wolves) == 1 else "wolf_propose", wolves)

    def legal_actions(self, seat: int) -> list[dict[str, Any]]:
        player = self._player(seat)
        shooting = self._state["phase"] == "hunter_shot" and player["role"] == "hunter"
        farewell = self._state["phase"] == "last_words"
        if self.finished or (not player["alive"] and not shooting and not farewell) or seat != self.next_actor():
            return []
        kind = self.PHASE_ACTION[self._state["phase"]]
        if kind == "witch":
            stock = self._state["potions"][str(seat)]
            potions = {"skip": []}
            victim = self._state["pending_kill"]
            if stock["save"] and victim is not None and victim != seat:
                potions["save"] = [victim]
            if stock["poison"]:
                potions["poison"] = [s for s in self._living() if s != seat]
            return [{"kind": "witch", "potions": potions}]
        if kind in {"speak", "wolf_message", "last_words"}:
            spec = {"kind": kind, "max_length": self.config.wolf_message_limit if kind == "wolf_message" else self.config.speech_limit}
            if kind == "last_words":
                spec["allow_empty"] = True
            return [spec]
        if kind == "protect":
            targets = [s for s in self._living() if s != self._state["last_guard"].get(str(seat))]
        elif kind in {"kill", "propose_kill"}:
            targets = [p["seat"] for p in self._state["players"]
                       if p["alive"] and p["role"] != "wolf"]
        elif self._state["phase"] == "runoff_vote":
            targets = list(self._state["tied"])
        else:
            targets = [s for s in self._living() if s != seat]
        spec = {"kind": kind, "targets": targets, "allow_abstain": kind in {"vote", "protect", "shoot", "duel"}}
        if kind == "propose_kill":
            spec["max_length"] = self.config.wolf_message_limit
        return [spec]

    def submit(self, seat: int, action: Action) -> None:
        """Validate the full action before mutation; only then advance the game."""
        allowed = self.legal_actions(seat)
        if not allowed:
            raise RuleViolation("当前不是该玩家的行动回合，或玩家已出局/游戏已结束")
        spec = allowed[0]
        validate_action(action, spec)

        state = self._state
        if action.kind == "protect":
            state["protected"] = action.target
            state["last_guard"][str(seat)] = action.target
            self._emit("guard_action", {"target": action.target}, [seat])
        elif action.kind == "shoot":
            state["hunter_used"].append(seat)
            if action.target is not None:
                self._player(action.target)["alive"] = False
            self._emit("hunter_shot", {"seat": seat, "target": action.target})
        elif action.kind == "duel":
            if action.target is not None:
                state["knight_used"].append(seat)
                success = self._player(action.target)["role"] == "wolf"
                self._player(action.target if success else seat)["alive"] = False
                self._emit("knight_duel", {"seat": seat, "target": action.target, "success": success})
                state["revision"] += 1
                if not self._check_winner():
                    if success:
                        self._next_night()
                    else:
                        self._day_next()
                return
            # Passing does not spend the once-per-game skill, but only one window per day.
            self._phase("day_speech", [seat])
            state["revision"] += 1
            return
        elif action.kind in {"speak", "last_words"}:
            self._emit(action.kind, {"seat": seat, "text": action.text})
        elif action.kind == "propose_kill":
            state["wolf_proposals"][str(seat)] = {"target": action.target, "text": action.text}
        elif action.kind == "wolf_message":
            state["wolf_replies"][str(seat)] = action.text
            if self._wolf_reply_mode == "sequential":
                self._emit("wolf_message", {"seat": seat, "text": action.text}, self._living("wolf"))
        elif action.kind == "kill":
            state["wolf_votes"][str(seat)] = action.target
            # Wolf target selections remain sealed until every wolf has selected.
        elif action.kind == "inspect":
            result = {"target": action.target, "is_wolf": self._player(action.target)["role"] == "wolf"}
            state["checks"].setdefault(str(seat), []).append({"day": state["day"], **result})
            self._emit("inspection", result, [seat])
        elif action.kind == "witch":
            if action.potion != "skip":
                state["potions"][str(seat)][action.potion] = False
                state["pending_save" if action.potion == "save" else "pending_poison"] = action.target
            self._emit("witch_action", {"potion": action.potion, "target": action.target}, [seat])
        else:
            state["ballots"][str(seat)] = action.target
        state["revision"] += 1
        state["cursor"] += 1
        if self.next_actor() is None:
            self._advance()

    def _advance(self) -> None:
        state = self._state
        phase = state["phase"]
        if phase == "last_words":
            self._after_death_words(set(state["last_words_shot_candidates"]), state["after_last_words"])
        elif phase == "guard":
            self._wolves_start()
        elif phase == "hunter_shot":
            if not self._check_winner():
                if state["after_shot"] == "night":
                    self._next_night()
                else:
                    self._start_day()
        elif phase == "wolf_propose":
            self._emit("wolf_proposals", {"proposals": state["wolf_proposals"]}, self._living("wolf"))
            targets = {p["target"] for p in state["wolf_proposals"].values()}
            if len(targets) == 1:
                self._resolve_wolf_target(
                    {seat: p["target"] for seat, p in state["wolf_proposals"].items()},
                    "unanimous_proposals",
                )
            else:
                self._phase("wolf_reply", self._living("wolf"))
        elif phase == "wolf_reply":
            if self._wolf_reply_mode == "batch":
                self._emit("wolf_replies", {"replies": state["wolf_replies"]}, self._living("wolf"))
            self._phase("wolf_vote", self._living("wolf"))
        elif phase == "wolf_vote":
            self._resolve_wolf_target(state["wolf_votes"], "final_vote")
        elif phase == "seer":
            self._witch_or_dawn()
        elif phase == "witch":
            self._dawn()
        elif phase == "day_speech":
            if self.config.knights:
                self._day_next()
            else:
                self._start_vote()
        elif phase in {"vote", "runoff_vote"}:
            self._settle_votes()
        elif phase == "runoff_speech":
            state["ballots"] = {}
            voters = [s for s in self._living() if s not in state["tied"]]
            self._phase("runoff_vote", voters)
            self._emit("voting_started", {"round": 2, "candidates": state["tied"]})
            if not voters:
                self._settle_votes()

    def _resolve_wolf_target(self, votes: dict[str, int], method: str) -> None:
        counts = Counter(votes.values())
        leaders = sorted(target for target, count in counts.items() if count == max(counts.values()))
        target = leaders[0] if len(leaders) == 1 else self._rng.choice(leaders)
        self._state["pending_kill"] = target
        self._emit("wolf_decision", {"votes": votes, "target": target, "method": method},
                   self._living("wolf"))
        seers = self._living("seer")
        if seers:
            self._phase("seer", seers)
        else:
            self._witch_or_dawn()

    def _witch_or_dawn(self) -> None:
        witches = [seat for seat in self._living("witch")
                   if any(self._state["potions"][str(seat)].values())]
        if witches:
            self._phase("witch", witches)
        else:
            self._dawn()

    def _dawn(self) -> None:
        state = self._state
        target = state["pending_kill"]
        poisoned = state["pending_poison"]
        dead = {target} if target is not None and target not in (state["pending_save"], state["protected"]) else set()
        if state["pending_poison"] is not None:
            dead.add(state["pending_poison"])
        for seat in dead:
            self._player(seat)["alive"] = False
        state.update(pending_kill=None, pending_save=None, pending_poison=None)
        self._emit("dawn", {"dead": sorted(dead)})
        self._death_words(dead if state["day"] == 1 else set(), dead - {poisoned}, "day")

    def _death_words(self, speakers: set[int], shot_candidates: set[int], after: str) -> None:
        if self.config.last_words and speakers:
            self._state["after_last_words"] = after
            self._state["last_words_shot_candidates"] = sorted(shot_candidates)
            self._phase("last_words", sorted(speakers))
            self._emit("last_words_started", {"seats": sorted(speakers)})
        else:
            self._after_death_words(shot_candidates, after)

    def _after_death_words(self, shot_candidates: set[int], after: str) -> None:
        self._state.update(after_last_words=None, last_words_shot_candidates=[])
        if not self._offer_shot(shot_candidates, after) and not self._check_winner():
            if after == "night":
                self._next_night()
            else:
                self._start_day()

    def _offer_shot(self, dead: set[int], after: str) -> bool:
        hunters = [s for s in sorted(dead) if self._player(s)["role"] == "hunter" and s not in self._state["hunter_used"]]
        if not hunters:
            return False
        self._state["after_shot"] = after
        self._phase("hunter_shot", hunters)
        self._emit("hunter_ready", {"seat": hunters[0]})
        return True

    def _start_day(self) -> None:
        if self.config.hunters or self.config.knights or self.config.last_words:
            self._emit("day_started", {"knight_available": bool(self.config.knights)})
        self._state["speech_remaining"] = self._living()
        if self.config.knights:
            self._day_next()
        else:
            self._phase("day_speech", self._living())

    def _day_next(self) -> None:
        remaining = self._state["speech_remaining"]
        while remaining:
            seat = remaining.pop(0)
            if self._player(seat)["alive"]:
                knight = self._player(seat)["role"] == "knight" and seat not in self._state["knight_used"]
                self._phase("knight_duel" if knight else "day_speech", [seat])
                return
        self._start_vote()

    def _start_vote(self) -> None:
        self._state["ballots"] = {}
        self._phase("vote", self._living())
        self._emit("voting_started", {"round": 1})

    def _settle_votes(self) -> None:
        state = self._state
        runoff = state["phase"] == "runoff_vote"
        counts = Counter(t for t in state["ballots"].values() if t is not None)
        self._emit("vote_result", {
            "round": 2 if runoff else 1, "votes": state["ballots"],
            "counts": {str(seat): count for seat, count in sorted(counts.items())},
        })
        leaders = sorted(s for s, count in counts.items() if count == max(counts.values())) if counts else []
        if len(leaders) == 1:
            self._player(leaders[0])["alive"] = False
            self._emit("eliminated", {"seat": leaders[0]})
            self._death_words({leaders[0]}, {leaders[0]}, "night")
        elif len(leaders) > 1 and not runoff:
            state["tied"] = leaders
            self._emit("runoff_started", {"candidates": leaders})
            self._phase("runoff_speech", leaders)
        else:
            self._emit("no_elimination", {"reason": "tie" if leaders else "abstain"})
            self._next_night()

    def _next_night(self) -> None:
        self._state["day"] += 1
        self._start_night()

    def _check_winner(self) -> bool:
        wolves = len(self._living("wolf"))
        if wolves == 0:
            winner = "good"
        elif self.config.win_condition == "eliminate_side":
            villagers = len(self._living("villager"))
            gods = sum(p["alive"] and p["role"] not in {"wolf", "villager"}
                       for p in self._state["players"])
            winner = "wolves" if villagers == 0 or gods == 0 else None
        else:
            good = len(self._living()) - wolves
            winner = "wolves" if wolves >= good else None
        if winner is None:
            return False
        self._state["winner"] = winner
        self._phase("finished", [])
        self._emit("game_over", {"winner": winner})
        return True

    def player_view(self, seat: int, *, public_only: bool = False) -> dict[str, Any]:
        """Detached, permission-filtered view; no seed, RNG, pending votes or kill."""
        player = self._player(seat)
        state = self._state
        events = []
        for event in state["events"]:
            if event["audience"] is None or (not public_only and seat in event["audience"]):
                # View-local IDs do not reveal gaps from others' private events.
                events.append({"id": len(events) + 1, "day": event["day"],
                               "kind": event["kind"], "data": deepcopy(event["data"])})
        night = state["phase"] in self.NIGHT_PHASES
        view = {
            "rules": self.config.public_rules(), "seat": seat, "role": player["role"],
            "alive": player["alive"], "day": state["day"],
            "phase": "night" if night else "day_speech" if state["phase"] == "knight_duel"
            and (public_only or seat != self.next_actor()) else state["phase"],
            "players": [{"seat": p["seat"], "alive": p["alive"]} for p in state["players"]],
            "legal_actions": self.legal_actions(seat), "events": events,
            "checks": [] if public_only else deepcopy(state["checks"].get(str(seat), [])),
            "wolf_teammates": [p["seat"] for p in state["players"]
                               if p["role"] == "wolf" and p["seat"] != seat]
            if player["role"] == "wolf" and not public_only else [],
            "winner": state["winner"],
        }
        if player["role"] == "witch" and not public_only:
            stock = state["potions"][str(seat)]
            view["witch"] = {"potions": deepcopy(stock)}
            # Only the current witch decision may reveal the knife victim, and only
            # while the antidote remains. No public subphase or death-cause leakage.
            if seat == self.next_actor() and state["phase"] == "witch" and stock["save"]:
                view["witch"]["night_victim"] = state["pending_kill"]
        if player["role"] == "guard" and not public_only:
            view["guard"] = {"last_target": state["last_guard"].get(str(seat))}
        if player["role"] == "knight" and not public_only:
            view["knight"] = {"used": seat in state["knight_used"]}
        # Do not reveal the night actor, subphase or global revision counter.
        if not night:
            view["current_actor"] = self.next_actor()
        if self.finished:
            view["revealed_roles"] = {str(p["seat"]): p["role"] for p in state["players"]}
        return view

    def replay(self) -> dict[str, Any]:
        if not self.finished:
            raise RuleViolation("完整复盘仅在对局结束后开放")
        return deepcopy({"winner": self._state["winner"],
                         "players": self._state["players"], "events": self._state["events"]})

    def snapshot(self) -> dict[str, Any]:
        """Trusted persistence only: contains all secrets, never a player API."""
        config = asdict(self.config)
        if not self._explicit_win_condition:
            config.pop("win_condition")
        return deepcopy({"snapshot_version": self._snapshot_version,
                         "config": config, "state": self._state,
                         **({"wolf_reply_mode": self._wolf_reply_mode} if self._wolf_reply_mode != "batch" else {}),
                         "rng_state": self._rng.getstate()})

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> GameEngine:
        """Restore a trusted engine-produced snapshot, including after JSON encoding."""
        data = deepcopy(snapshot)
        if data.get("snapshot_version") not in {2, 3, 4, 5, cls.SNAPSHOT_VERSION}:
            raise RuleViolation("不支持的存档版本")
        if data["snapshot_version"] == 2 and data["config"].get("version") != "six-player-v2":
            raise RuleViolation("旧存档版本不能使用新版规则")
        engine = cls.__new__(cls)
        engine._wolf_reply_mode = data.get("wolf_reply_mode", "batch")
        if engine._wolf_reply_mode not in {"batch", "sequential"}:
            raise RuleViolation("未知狼队回应方式")
        config = data["config"]
        # v5 prepared actions hash the old snapshot shape. Keep that shape across
        # restore/submit so a schema upgrade cannot discard paid buffered work.
        engine._snapshot_version = data["snapshot_version"] if data["snapshot_version"] == 5 else cls.SNAPSHOT_VERSION
        engine._explicit_win_condition = "win_condition" in config
        # Missing fields belong to historical parity games, never migrate their rules.
        config.setdefault("win_condition", "parity")
        if config.get("version") == "six-player-v2":
            config.setdefault("witches", 0)
        engine.config = GameConfig(**config)
        engine._state = data["state"]
        for key, default in {"last_guard": {}, "protected": None, "knight_used": [], "hunter_used": [], "after_shot": None, "speech_remaining": [], "after_last_words": None, "last_words_shot_candidates": []}.items():
            engine._state.setdefault(key, default)
        if engine.config.witches == 0:
            engine._state.setdefault("potions", {})
            engine._state.setdefault("pending_save", None)
            engine._state.setdefault("pending_poison", None)
        engine._rng = random.Random()

        def tuples(value: Any) -> Any:
            return tuple(tuples(v) for v in value) if isinstance(value, (list, tuple)) else value

        engine._rng.setstate(tuples(data["rng_state"]))
        return engine
