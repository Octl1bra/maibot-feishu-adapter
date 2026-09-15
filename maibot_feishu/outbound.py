"""MaiBot 旧版 maim_message 出站 MessageBase（dict）→ 飞书消息。

MaiBot 1.2.5 出站形态（src/common/data_models/mai_message_data_model.py::to_maim_message）：
  message_info.group_info.group_id = 群 chat_id（群聊）；私聊时 group_info 为空，
  真正的收件人在 additional_config.platform_io_target_user_id / receiver_info.user_info.user_id，
  而 message_info.user_info 是机器人自己。
  seglist 里 text 是字符串，image/emoji 是 base64，at 是 target_user_id 字符串，reply 是被引用消息 id。
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .feishu_api import FeishuAPI, FeishuAPIError

logger = logging.getLogger("feishu.outbound")


@dataclass
class OutboundPlan:
    receive_id: str
    receive_id_type: str  # "chat_id" | "open_id"
    reply_to: Optional[str] = None
    text: str = ""
    images: list[bytes] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.text.strip() and not self.images


def _iter_segments(segment: Any):
    if not isinstance(segment, dict):
        return
    seg_type = segment.get("type")
    data = segment.get("data")
    if seg_type == "seglist" and isinstance(data, list):
        for item in data:
            yield from _iter_segments(item)
    else:
        yield seg_type, data


def _at_target(data: Any) -> tuple[str, str]:
    if isinstance(data, dict):
        target = str(data.get("target_user_id") or data.get("user_id") or data.get("id") or "").strip()
        name = str(data.get("target_user_nickname") or data.get("nickname") or data.get("name") or "").strip()
        return target, name
    return str(data or "").strip(), ""


def plan_outbound(message: dict[str, Any], bot_open_id: str) -> Optional[OutboundPlan]:
    info = message.get("message_info") or {}
    group_info = info.get("group_info") or None
    additional = info.get("additional_config") or {}
    if not isinstance(additional, dict):
        additional = {}

    if group_info and group_info.get("group_id"):
        plan = OutboundPlan(receive_id=str(group_info["group_id"]), receive_id_type="chat_id")
    else:
        target = str(additional.get("platform_io_target_user_id") or "").strip()
        if not target:
            receiver = info.get("receiver_info") or {}
            receiver_user = receiver.get("user_info") or {}
            target = str(receiver_user.get("user_id") or "").strip()
        if not target:
            sender_user = (info.get("user_info") or {}).get("user_id")
            if sender_user and str(sender_user) != bot_open_id:
                target = str(sender_user)
        if not target:
            logger.warning("出站消息找不到收件人，丢弃: %s", json.dumps(info, ensure_ascii=False)[:300])
            return None
        plan = OutboundPlan(receive_id=target, receive_id_type="open_id")

    text_parts: list[str] = []
    for seg_type, data in _iter_segments(message.get("message_segment")):
        if seg_type == "text":
            text_parts.append(str(data or ""))
        elif seg_type == "at":
            target, name = _at_target(data)
            if target:
                text_parts.append(f'<at user_id="{target}">{name}</at> ')
        elif seg_type == "reply":
            if data:
                plan.reply_to = str(data)
        elif seg_type in {"image", "emoji"}:
            if isinstance(data, str) and data:
                try:
                    plan.images.append(base64.b64decode(data, validate=False))
                except (binascii.Error, ValueError):
                    plan.skipped.append(f"{seg_type}(bad base64)")
        elif seg_type == "voice":
            plan.skipped.append("voice")
        elif seg_type == "file":
            name = data.get("file_name") if isinstance(data, dict) else ""
            text_parts.append(f"[文件:{name}]" if name else "[文件]")
        elif seg_type == "dict":
            plan.skipped.append("dict")
        else:
            plan.skipped.append(str(seg_type))
    plan.text = "".join(text_parts).strip()
    return plan


async def deliver(plan: OutboundPlan, api: FeishuAPI) -> list[str]:
    """按计划发到飞书；文本（含 @）走一条消息，每张图各一条。返回飞书 message_id 列表。"""
    sent: list[str] = []
    if plan.skipped:
        logger.info("出站消息中忽略了不支持的段: %s", ",".join(plan.skipped))
    if plan.empty:
        return sent

    async def _send(msg_type: str, content: dict[str, Any], use_reply: bool) -> None:
        payload = json.dumps(content, ensure_ascii=False)
        if use_reply and plan.reply_to:
            try:
                sent.append(await api.reply_message(plan.reply_to, msg_type, payload))
                return
            except FeishuAPIError as exc:
                # 被引用的消息可能已撤回；退化成普通发送
                logger.warning("引用回复失败(%s)，改为直接发送", exc)
        sent.append(await api.send_message(plan.receive_id, plan.receive_id_type, msg_type, payload))

    if plan.text:
        await _send("text", {"text": plan.text}, use_reply=True)
    for index, image in enumerate(plan.images):
        try:
            image_key = await api.upload_image(image)
        except FeishuAPIError as exc:
            logger.warning("上传图片失败: %s", exc)
            continue
        await _send("image", {"image_key": image_key}, use_reply=(index == 0 and not plan.text))
    return sent
