"""Role fundamentals from an actor's filtered view, never from hidden game state."""

from copy import deepcopy


def decision_facts(view: dict) -> dict:
    """Keep claims out of confirmed knowledge; record the source of each conclusion."""
    good, wolves, roles, sources = set(), set(), {}, []
    seats = {p["seat"] for p in view["players"]}
    living = {p["seat"] for p in view["players"] if p["alive"]}

    def confirm(seat: int, is_wolf: bool, source: str, *, private: bool, event_id: int | None = None) -> None:
        if seat not in seats:
            return
        (wolves if is_wolf else good).add(seat)
        sources.append({"seat": seat, "source": source, "visibility": "private" if private else "public",
                        **({"event_id": event_id} if event_id is not None else {})})

    own, role = view["seat"], view["role"]
    roles[str(own)] = role
    confirm(own, role == "wolf", "own_role", private=True)
    if role == "wolf":
        team = {own, *view["wolf_teammates"]}
        for seat in sorted(team - {own}):
            confirm(seat, True, "wolf_team", private=True)
        if len(team) == view["rules"]["wolves"]:
            for seat in sorted(seats - team):
                confirm(seat, False, "outside_complete_wolf_team", private=True)
    if role == "seer":
        for check in view["checks"]:
            confirm(check["target"], check["is_wolf"], "own_inspection", private=True)
    if role == "witch":
        victim = view.get("witch", {}).get("night_victim")
        if victim is not None:
            confirm(victim, False, "own_night_victim_no_friendly_knife", private=True)
    ballots, own_votes, public_roles = [], [], {}
    for event in view["events"]:
        kind, data = event["kind"], event["data"]
        if role == "witch" and kind == "witch_action" and data["potion"] == "save":
            confirm(data["target"], False, "own_saved_victim_no_friendly_knife", private=True,
                    event_id=event.get("id"))
        if kind in {"hunter_ready", "hunter_shot", "knight_duel"}:
            revealed = "knight" if kind == "knight_duel" else "hunter"
            roles[str(data["seat"])] = revealed
            public_roles[str(data["seat"])] = revealed
            confirm(data["seat"], False, kind, private=False, event_id=event.get("id"))
            if kind == "knight_duel":
                confirm(data["target"], data["success"], "knight_duel_result", private=False,
                        event_id=event.get("id"))
                if data["success"]:
                    roles[str(data["target"])] = "wolf"
                    public_roles[str(data["target"])] = "wolf"
        if kind == "vote_result":
            ballot = {"day": event["day"], **deepcopy(data),
                      **({"event_id": event["id"]} if "id" in event else {})}
            ballots.append(ballot)
            if str(own) in data["votes"]:
                own_votes.append({"day": event["day"], "round": data.get("round", 1),
                                  "target": data["votes"][str(own)],
                                  **({"event_id": event["id"]} if "id" in event else {})})

    # Only a settled, ongoing game guarantees a living wolf. Death windows can
    # temporarily contain zero wolves while a farewell or hunter shot is pending.
    pending = view["phase"] in {"last_words", "hunter_shot"}
    if not pending and view["winner"] is None:
        unknown = living - good - wolves
        alive_wolves = living & wolves
        upper = min(view["rules"]["wolves"] - len(wolves - living),
                    max(0, len(living) - 2) if view["rules"].get("win_condition") == "eliminate_side"
                    else (len(living) - 1) // 2)
        if len(unknown) == 1 and not alive_wolves:
            confirm(next(iter(unknown)), True, "ongoing_game_requires_living_wolf", private=True)
        elif upper >= 1 and len(alive_wolves) == upper:
            for seat in sorted(unknown):
                confirm(seat, False, "living_wolf_upper_bound_filled", private=True)

    return {
        "confirmed_good": sorted(good), "confirmed_wolves": sorted(wolves),
        "known_roles": roles, "sources": sources, "public_role_reveals": public_roles,
        "dead_without_public_role_reveal": sorted(seats - living - {int(s) for s in public_roles}),
        "recent_vote_results": ballots[-2:], "own_votes": own_votes,
        "notice": "这里只列本人可确认的事实；口头身份、遗言认牌、普通死亡和猎人枪口不是翻牌。私密来源不等于全场已知。",
    }


def constrain_action(spec: dict, view: dict, facts: dict) -> dict:
    """AI-only factual guardrails; engine and human legal actions stay unchanged."""
    spec = deepcopy(spec)
    good, wolves = set(facts["confirmed_good"]), set(facts["confirmed_wolves"])
    if view["role"] != "wolf":
        if spec["kind"] in {"vote", "shoot", "duel"}:
            targets = [s for s in spec["targets"] if s not in good]
            spec["targets"] = [s for s in targets if s in wolves] or targets
        elif spec["kind"] == "witch" and "poison" in spec["potions"]:
            targets = [s for s in spec["potions"]["poison"] if s not in good]
            if targets:
                spec["potions"]["poison"] = [s for s in targets if s in wolves] or targets
            else:
                del spec["potions"]["poison"]
    if spec["kind"] == "inspect":
        checked = {c["target"] for c in view["checks"]}
        spec["targets"] = [s for s in spec["targets"] if s not in checked] or spec["targets"]
    return spec
