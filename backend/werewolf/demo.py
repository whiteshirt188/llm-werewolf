"""Offline demonstration. All six seats are simulated, including the human seat."""

from __future__ import annotations

import argparse
import json

from .engine import Action, GameConfig, GameEngine
from .roster import RosterConfig


def simulated_action(view: dict) -> Action:
    """Deterministic, intentionally simple policy using only a player's view."""
    spec = view["legal_actions"][0]
    kind = spec["kind"]
    if kind in {"duel", "shoot"}:
        return Action(kind, target=None)
    if kind == "witch":
        if "save" in spec["potions"]:
            return Action(kind, target=spec["potions"]["save"][0], potion="save")
        # No omniscient poisoning: the offline policy only uses its visible facts.
        return Action(kind, potion="skip")
    if kind == "wolf_message":
        return Action(kind, text="先看白天发言，再协调目标。")
    if kind in {"speak", "last_words"}:
        checks = view["checks"]
        if checks:
            check = checks[-1]
            verdict = "狼人" if check["is_wolf"] else "好人"
            text = f"我是预言家，查验 {check['target']} 号是{verdict}。"
        else:
            text = "我会结合发言和公开票型判断，暂不轻信身份声明。"
        return Action(kind, text=text)
    targets = spec["targets"]
    if not targets and spec["allow_abstain"]:
        return Action(kind, target=None)
    if kind == "inspect":
        checked = {item["target"] for item in view["checks"]}
        targets = [s for s in targets if s not in checked] or targets
    if kind == "vote":
        confirmed_wolves = {item["target"] for item in view["checks"] if item["is_wolf"]}
        targets = [s for s in targets if s in confirmed_wolves] or targets
        if view["role"] == "wolf":
            targets = [s for s in targets if s not in view["wolf_teammates"]] or targets
    target = targets[(view["day"] + view["seat"]) % len(targets)]
    return Action(kind, target=target,
                  text="建议先处理这位玩家，观察明天的票型。" if kind == "propose_kill" else None)


def run_demo(seed: int = 7, config: GameConfig | None = None) -> GameEngine:
    game = GameEngine(seed, config)
    for _ in range(200):
        if game.finished:
            return game
        seat = game.next_actor()
        game.submit(seat, simulated_action(game.player_view(seat)))
    raise RuntimeError("模拟超过 200 次行动，检查引擎是否停止推进")


def main() -> None:
    parser = argparse.ArgumentParser(description="狼人杀离线规则演示（全席模拟，不调用模型）")
    parser.add_argument("--seed", type=int, default=7, help="随机种子，默认 7")
    parser.add_argument("--players", type=int, choices=(6, 9), default=6)
    parser.add_argument("--json", action="store_true", help="结束后输出完整 JSON 复盘")
    args = parser.parse_args()
    game = run_demo(args.seed, RosterConfig(players=args.players, wolves=3 if args.players == 9 else 2, hunters=1 if args.players == 9 else 0).engine_config())
    replay = game.replay()
    if args.json:
        print(json.dumps(replay, ensure_ascii=False, indent=2))
        return
    print(f"{game.config.players} 人狼人杀 · 离线模拟 · seed={args.seed}（无 API 调用）")
    labels = {
        "game_started": "对局开始", "night_started": "天黑请闭眼",
        "dawn": "天亮公布死亡", "speak": "公开发言", "voting_started": "开始投票",
        "vote_result": "公布票型", "runoff_started": "平票补充发言",
        "eliminated": "投票放逐", "no_elimination": "无人出局", "game_over": "对局结束",
    }
    for event in replay["events"]:
        if event["audience"] is None:
            print(f"[第 {event['day']} 天] {labels.get(event['kind'], event['kind'])} "
                  f"{json.dumps(event['data'], ensure_ascii=False)}")
    print("胜方：" + ("好人" if replay["winner"] == "good" else "狼人"))
    names = {"wolf": "狼人", "seer": "预言家", "witch": "女巫", "villager": "村民", "hunter": "猎人", "guard": "守卫", "knight": "骑士"}
    print("赛后身份：" + "，".join(f"{p['seat']}号={names[p['role']]}" for p in replay["players"]))


if __name__ == "__main__":
    main()
