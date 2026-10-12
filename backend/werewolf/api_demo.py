"""Drive the human seat through a running local mock API; no model API calls."""

import argparse
import asyncio
import time
from dataclasses import asdict
from uuid import uuid4

import httpx

from .demo import simulated_action


async def run(base_url: str, seed: int, human_seat: int) -> None:
    # This local demo must not send localhost requests through environment proxies.
    async with httpx.AsyncClient(base_url=base_url, timeout=30, trust_env=False) as client:
        created = await client.post("/api/games", json={"seed": seed, "human_seat": human_seat, "playback_mode": "auto"})
        created.raise_for_status()
        data = created.json()
        game_id = data["game"]["game_id"]
        client.headers["Authorization"] = f"Bearer {data['session_token']}"
        print(f"API 离线模拟：人类席位 {human_seat} 号也由演示脚本操作，对局 {game_id}")
        # Production pacing deliberately takes minutes; do not fail after the old ~100 s loop budget.
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            response = await client.get(f"/api/games/{game_id}")
            response.raise_for_status()
            state = response.json()
            if state["status"] == "finished":
                replay = await client.get(f"/api/games/{game_id}/replay")
                replay.raise_for_status()
                print("胜方：" + ("好人" if replay.json()["winner"] == "good" else "狼人"))
                print(f"已持久化 {len(replay.json()['usage'])} 次模拟 Agent 调用记录，无真实 API 调用。")
                return
            if state["status"] == "paused":
                raise RuntimeError(f"对局已暂停：{state['pause_reason']}，请手动恢复")
            if state["turn_version"]:
                result = await client.post(f"/api/games/{game_id}/actions", json={
                    "operation_id": uuid4().hex, "expected_version": state["turn_version"],
                    "action": asdict(simulated_action(state["view"])),
                })
                result.raise_for_status()
            else:
                await asyncio.sleep(0.05)
        raise RuntimeError("演示等待超限，请检查服务状态")


def main() -> None:
    parser = argparse.ArgumentParser(description="运行中的本机 FastAPI 离线演示")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--seed", type=int, default=38)
    parser.add_argument("--human-seat", type=int, default=3, choices=range(1, 7))
    args = parser.parse_args()
    try:
        asyncio.run(run(args.base_url, args.seed, args.human_seat))
    except httpx.ConnectError:
        parser.exit(1, "无法连接 API 服务。请在第一个终端运行 make api 并保持运行，"
                    "不要按 Ctrl+C；在第二个终端运行 make api-demo。\n")
    except httpx.RequestError:
        parser.exit(1, "API 请求失败或超时，请检查服务是否仍在运行及 --base-url 是否正确。\n")
    except httpx.HTTPStatusError as exc:
        parser.exit(1, f"API 返回 HTTP {exc.response.status_code}，请检查服务日志。"
                    "若为 409，可能已有未结束对局，需要使用原会话恢复。\n")


if __name__ == "__main__":
    main()
