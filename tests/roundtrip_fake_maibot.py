"""协议层回归：用 maim_message 自带的 MessageServer 扮演 MaiBot，验证
   适配器 → MaiBot 入站投递 和 MaiBot → 适配器 出站回调 两个方向都能过真实 WebSocket。
   不依赖飞书凭据。运行：python tests/roundtrip_fake_maibot.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maim_message import MessageBase, MessageServer, Seg  # noqa: E402

from maibot_feishu.inbound import BotIdentity, RecentIds, event_to_message  # noqa: E402
from maibot_feishu.maibot_link import MaiBotLink  # noqa: E402
from maibot_feishu.outbound import plan_outbound  # noqa: E402
from tests.fakes import FakeFeishuAPI, make_event, mention  # noqa: E402

PORT = 18765
PLATFORM = "feishu"
logging.basicConfig(level=logging.WARNING)


async def main() -> int:
    received_by_server: asyncio.Queue = asyncio.Queue()
    received_by_adapter: asyncio.Queue = asyncio.Queue()

    # 1. 假 MaiBot：收到消息后原样"回一句"
    server = MessageServer(host="127.0.0.1", port=PORT)

    async def on_server_message(message_dict: dict):
        await received_by_server.put(message_dict)
        info = message_dict["message_info"]
        reply = MessageBase(
            message_info=type(MessageBase.from_dict(message_dict).message_info)(
                platform=PLATFORM,
                message_id="mm_reply_1",
                time=info["time"],
                user_info={"platform": PLATFORM, "user_id": "ou_bot", "user_nickname": "麦麦", "user_cardname": None},
                group_info=info.get("group_info"),
                additional_config={"platform_io_target_group_id": (info.get("group_info") or {}).get("group_id")},
            ),
            message_segment=Seg(type="seglist", data=[Seg(type="reply", data=info["message_id"]), Seg(type="text", data="收到啦")]),
        )
        await server.send_message(reply)

    server.register_message_handler(on_server_message)
    server_task = asyncio.create_task(server.run())
    await asyncio.sleep(1.0)

    # 2. 适配器侧
    async def on_outbound(message: dict):
        await received_by_adapter.put(message)

    link = MaiBotLink(PLATFORM, f"ws://127.0.0.1:{PORT}/ws", "", on_outbound)
    link.start()
    for _ in range(50):
        if link.connected:
            break
        await asyncio.sleep(0.1)
    assert link.connected, "适配器没连上假 MaiBot"

    bot = BotIdentity(platform=PLATFORM, open_id="ou_bot", name="麦麦")
    api = FakeFeishuAPI()
    ev = make_event(content='{"text":"@_user_1 在吗"}', mentions=[mention("@_user_1", "ou_bot", "麦麦")])
    msg = await event_to_message(ev, api, bot, frozenset(), RecentIds())
    await link.send(msg)

    got = await asyncio.wait_for(received_by_server.get(), timeout=5)
    assert got["message_info"]["platform"] == PLATFORM
    assert got["message_info"]["additional_config"]["at_bot"] is True
    assert got["message_segment"]["data"][0]["type"] == "at"
    print("✔ 入站：假 MaiBot 收到了 seglist =", [s["type"] for s in got["message_segment"]["data"]])

    back = await asyncio.wait_for(received_by_adapter.get(), timeout=5)
    plan = plan_outbound(back, "ou_bot")
    assert plan is not None and plan.receive_id == "oc_group1" and plan.reply_to == "om_1" and plan.text == "收到啦"
    print("✔ 出站：适配器收到回包并规划为", plan.receive_id_type, plan.receive_id, "reply_to", plan.reply_to, "text", plan.text)

    await link.stop()
    await server.stop()
    server_task.cancel()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
