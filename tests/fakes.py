"""测试用的假飞书 API 与假事件对象。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class FakeFeishuAPI:
    def __init__(self):
        self.downloads: list[tuple[str, str, str]] = []
        self.uploads: list[bytes] = []
        self.sent: list[dict[str, Any]] = []
        self.replied: list[dict[str, Any]] = []
        self.fail_reply = False

    async def user_name(self, open_id: str) -> str:
        return {"ou_alice": "Alice", "ou_bob": "Bob"}.get(open_id, f"user{open_id[-3:]}")

    async def chat_name(self, chat_id: str) -> str:
        return "测试群" if chat_id == "oc_group1" else f"群{chat_id[-3:]}"

    async def download_message_resource(self, message_id: str, file_key: str, resource_type: str = "image") -> bytes:
        self.downloads.append((message_id, file_key, resource_type))
        return b"\x89PNGfake"

    async def upload_image(self, image_bytes: bytes) -> str:
        self.uploads.append(image_bytes)
        return f"img_key_{len(self.uploads)}"

    async def send_message(self, receive_id: str, receive_id_type: str, msg_type: str, content: str) -> str:
        self.sent.append({"receive_id": receive_id, "receive_id_type": receive_id_type, "msg_type": msg_type, "content": content})
        return f"om_sent_{len(self.sent)}"

    async def reply_message(self, message_id: str, msg_type: str, content: str) -> str:
        if self.fail_reply:
            from maibot_feishu.feishu_api import FeishuAPIError

            raise FeishuAPIError(230011, "message not found", "reply")
        self.replied.append({"message_id": message_id, "msg_type": msg_type, "content": content})
        return f"om_reply_{len(self.replied)}"


def make_event(
    *,
    message_id: str = "om_1",
    chat_id: str = "oc_group1",
    chat_type: str = "group",
    message_type: str = "text",
    content: str = '{"text":"hello"}',
    sender_open_id: str = "ou_alice",
    sender_type: str = "user",
    mentions: list[Any] | None = None,
    parent_id: str = "",
    create_time: str = "1757900000000",
):
    """模仿 lark_oapi 的 P2ImMessageReceiveV1 对象结构（属性访问）。"""
    return SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id=sender_open_id, user_id="", union_id=""),
                sender_type=sender_type,
                tenant_key="t",
            ),
            message=SimpleNamespace(
                message_id=message_id,
                root_id="",
                parent_id=parent_id,
                create_time=create_time,
                chat_id=chat_id,
                chat_type=chat_type,
                message_type=message_type,
                content=content,
                mentions=mentions or [],
            ),
        )
    )


def mention(key: str, open_id: str, name: str):
    return SimpleNamespace(key=key, id=SimpleNamespace(open_id=open_id, user_id="", union_id=""), name=name, tenant_key="t")
