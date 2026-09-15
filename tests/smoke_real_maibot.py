"""对真 MaiBot（本地 ws://127.0.0.1:8000/ws）做入站冒烟：投一条 @机器人 的群消息，等出站回包。
   运行：python tests/smoke_real_maibot.py [等待秒数]
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maibot_feishu.inbound import BotIdentity, RecentIds, event_to_message  # noqa: E402
from maibot_feishu.maibot_link import MaiBotLink  # noqa: E402
from maibot_feishu.outbound import plan_outbound  # noqa: E402
from tests.fakes import FakeFeishuAPI, make_event, mention  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
WAIT = int(sys.argv[1]) if len(sys.argv) > 1 else 90


async def main() -> int:
    outbound: asyncio.Queue = asyncio.Queue()

    async def on_outbound(message: dict):
        await outbound.put(message)

    link = MaiBotLink("feishu", "ws://127.0.0.1:8000/ws", "", on_outbound)
    link.start()
    for _ in range(100):
        if link.connected:
            break
        await asyncio.sleep(0.1)
    if not link.connected:
        print("✘ 连不上真 MaiBot 的 8000 端口")
        return 1
    print("✔ 已连上真 MaiBot 旧版 WS")

    bot = BotIdentity(platform="feishu", open_id="ou_bot", name="麦麦")
    api = FakeFeishuAPI()
    for i, (text, mentions) in enumerate(
        [
            ("大家早", []),
            ("@_user_1 你在吗？", [mention("@_user_1", "ou_bot", "麦麦")]),
        ]
    ):
        ev = make_event(message_id=f"om_smoke_{i}", content=json.dumps({"text": text}), mentions=mentions)
        msg = await event_to_message(ev, api, bot, frozenset(), RecentIds())
        await link.send(msg)
        print(f"→ 已投递: {text}")
        await asyncio.sleep(1)

    try:
        back = await asyncio.wait_for(outbound.get(), timeout=WAIT)
    except asyncio.TimeoutError:
        print(f"… {WAIT}s 内没有等到出站回包（模型桩可能没让规划器产出动作；入站是否被接收看 MaiBot 日志）")
        await link.stop()
        return 2
    print("✔ 收到 MaiBot 出站原始消息:", json.dumps(back, ensure_ascii=False)[:600])
    plan = plan_outbound(back, "ou_bot")
    print("✔ 规划:", plan.receive_id_type, plan.receive_id, "reply_to", plan.reply_to, "text", plan.text[:80], "images", len(plan.images))
    await link.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
