"""Load project-owned instructions from a fixed manifest and freeze their content."""

import re
from dataclasses import dataclass
from pathlib import Path

from .profiles import content_hash


class RulebookError(ValueError):
    pass


@dataclass(frozen=True)
class Rulebook:
    files: tuple[tuple[str, str], ...]
    ROLE_COUNTS = (("wolf", "wolves", "狼人"), ("seer", "seers", "预言家"),
                   ("witch", "witches", "女巫"), ("hunter", "hunters", "猎人"),
                   ("guard", "guards", "守卫"), ("knight", "knights", "骑士"),
                   ("villager", "villagers", "村民"))
    MANIFEST = ("游戏.md", "rules-v2/规则.md", *(f"roles/werewolf-{role}/身份.md" for role in
                 ("wolf", "seer", "villager", "witch", "hunter", "guard", "knight")))
    LEGACY_MANIFEST = (
        "SKILL.md", "roles/werewolf-wolf/SKILL.md", "roles/werewolf-seer/SKILL.md",
        "roles/werewolf-villager/SKILL.md", "roles/werewolf-witch/SKILL.md",
        "references/phases/night.md", "references/phases/day.md",
        "roles/werewolf-hunter/SKILL.md", "roles/werewolf-guard/SKILL.md", "roles/werewolf-knight/SKILL.md",
    )

    @classmethod
    def load(cls, root: Path | None = None, *, legacy: bool = False) -> Rulebook:
        root = root or Path(__file__).resolve().parents[2] / "skills" / "werewolf-player"
        files = []
        for name in cls.LEGACY_MANIFEST if legacy else cls.MANIFEST:
            try:
                text = (root / name).read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise RulebookError(f"无法加载必要规则资料：{name}") from exc
            if not text.strip():
                raise RulebookError(f"必要规则资料为空：{name}")
            files.append((name, text))
        return cls(tuple(files))

    @property
    def fingerprint(self) -> str:
        return content_hash(dict(self.files))

    def select(self, role: str, action_kind: str, rules: dict | None = None) -> tuple[tuple[str, str], ...]:
        if role not in {"wolf", "seer", "villager", "witch", "hunter", "guard", "knight"}:
            raise RulebookError("未知角色")
        if action_kind not in {"opening_plan", "propose_kill", "wolf_message", "kill", "inspect", "witch", "speak", "vote", "protect", "shoot", "duel", "last_words"}:
            raise RulebookError("未知行动")
        files = dict(self.files)
        if "游戏.md" in files or "规则.md" in files:
            rule_path = "rules-v2/规则.md" if "rules-v2/规则.md" in files else "规则.md"
            names = ("游戏.md", f"roles/werewolf-{role}/身份.md", rule_path)
            if any(not files.get(name, "").strip() for name in names) or len(files) != len(self.files):
                raise RulebookError("本次行动缺少必要游戏、身份或规则正文")
            replacements = {}
            if rules is not None:
                loss = ("村民全灭或神职全灭" if rules.get("win_condition") == "eliminate_side"
                        else "存活狼人不少于好人")
                replacements = {"{胜负规则}": f"狼人全灭则好人胜；{loss}则狼人胜。",
                                "{本局胜负}": (f"{loss}则你们获胜，狼人全灭则失败。" if role == "wolf"
                                            else f"狼人全灭则你们获胜，{loss}则失败。")}
            def render(text):
                for source, replacement in replacements.items():
                    text = text.replace(source, replacement)
                if "{本局配置}" in text:
                    # Only opt-in templates are rendered, leaving previously frozen v9 documents exact.
                    known_roster = rules is not None and all(key in rules for _, key, _ in self.ROLE_COUNTS)
                    roster = "本局阵容以 state.rules 为准。"
                    if known_roster:
                        present = "、".join(f"{rules[key]}名{label}" for _, key, label in self.ROLE_COUNTS if rules[key])
                        absent = "、".join(label for _, key, label in self.ROLE_COUNTS if not rules[key])
                        roster = f"本局{rules['players']}人：{present}。"
                        if absent:
                            roster += f"没有{absent}，这些身份的技能不会发生。"
                        roster += "遗言开启。" if rules.get("last_words") else "遗言关闭。"
                    text = text.replace("{本局配置}", roster)
                    for role_name, key, _ in self.ROLE_COUNTS:
                        pattern = rf"<!--{role_name}-->(.*?)<!--/{role_name}-->"
                        text = re.sub(pattern, r"\1" if not known_roster or rules[key] else "",
                                      text, flags=re.DOTALL)
                return text
            return tuple(("规则.md" if name == rule_path else name, render(files[name])) for name in names)
        phase = "day" if action_kind in {"speak", "vote", "shoot", "duel", "last_words"} else "night"
        # Old games select their frozen role documents without consulting new files.
        role_path = f"roles/werewolf-{role}/SKILL.md"
        if role_path not in dict(self.files):
            role_path = f"references/roles/{role}.md"
        wanted = {"SKILL.md", role_path, f"references/phases/{phase}.md"}
        selected = tuple((name, text) for name, text in self.files if name in wanted)
        if {name for name, _ in selected} != wanted or len(selected) != 3 or any(not text.strip() for _, text in selected):
            raise RulebookError("本次行动缺少必要 Skill、角色或阶段上下文")
        return selected
