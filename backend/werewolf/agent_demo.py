"""Offline Player Agent demo and explicit, local private-request preview."""

import argparse
import asyncio
import json

from .agent import PlayerAgent
from .engine import GameEngine
from .mock_adapter import MockAdapter
from .profiles import ModelConnection, SeatProfile, persona_preset
from .prompts import PromptBuilder
from .rulebook import Rulebook


async def run_agent_demo(seed: int = 38, preset: str = "冷静分析") -> tuple[GameEngine, dict[int, PlayerAgent]]:
    game = GameEngine(seed)
    connection = ModelConnection(id="shared-mock", provider="mock", model="offline-policy",
                                 base_url="mock://local")
    builder = PromptBuilder(Rulebook.load())
    agents = {s: PlayerAgent(SeatProfile(seat=s, connection_id=connection.id,
                                       persona=persona_preset(preset)), connection, builder) for s in range(1, 7)}
    adapter = MockAdapter()
    for _ in range(200):
        if game.finished:
            return game, agents
        seat = game.next_actor()
        action = await agents[seat].decide(game.player_view(seat), adapter)
        game.submit(seat, action)
    raise RuntimeError("模拟未在行动上限内结束")


async def preview(seed: int, role: str, preset: str) -> dict:
    """Developer preview containing one player's private info; never a public feed."""
    game = GameEngine(seed)
    connection = ModelConnection(id="preview", provider="mock", model="offline-policy", base_url="mock://local")
    builder = PromptBuilder(Rulebook.load())
    adapter = MockAdapter()
    for _ in range(200):
        if game.finished:
            raise ValueError("该种子下此身份没有行动机会，请换一个种子")
        seat = game.next_actor()
        view = game.player_view(seat)
        agent = PlayerAgent(SeatProfile(seat=seat, connection_id=connection.id,
                                       persona=persona_preset(preset)), connection, builder)
        request = agent.prepare(view)
        if view["role"] == role:
            return {"private_preview": True, "seat": seat, "role": role,
                    "messages": request.wire_messages(), "max_output_tokens": request.max_output_tokens,
                    "input_characters": request.input_characters, "warnings": request.warnings,
                    "configuration_fingerprint": request.configuration_fingerprint,
                    "rulebook_fingerprint": request.rulebook_fingerprint}
        game.submit(seat, await agent.decide(view, adapter))
    raise RuntimeError("预览未找到行动")


def main() -> None:
    parser = argparse.ArgumentParser(description="Player Agent 离线演示；无 API 调用、无资格授予")
    parser.add_argument("--seed", type=int, default=38)
    parser.add_argument("--persona", default="冷静分析",
                        choices=["冷静分析", "简短直接", "话痨", "毒舌", "戏精"])
    parser.add_argument("--preview-role", choices=["wolf", "seer", "witch", "villager"],
                        help="开发用私密请求预览，请勿当作公开日志分享")
    args = parser.parse_args()
    if args.preview_role:
        print(json.dumps(asyncio.run(preview(args.seed, args.preview_role, args.persona)),
                         ensure_ascii=False, indent=2))
        return
    game, agents = asyncio.run(run_agent_demo(args.seed, args.persona))
    print(f"Player Agent 离线模拟 · seed={args.seed} · 六席共用一个模拟连接")
    print("胜方：" + ("好人" if game.replay()["winner"] == "good" else "狼人"))
    for seat, agent in agents.items():
        print(f"{seat}号：{len(agent.records)} 次独立决策，个性={agent.profile.persona.label}")
    print("所有调用均为模拟。实际 token 用量未知，不估作零；无 API 费用和资格测试。")


if __name__ == "__main__":
    main()
