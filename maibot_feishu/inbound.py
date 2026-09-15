"""飞书 im.message.receive_v1 事件 → MaiBot 旧版 maim_message MessageBase。

MaiBot 侧的解析规则（1.2.5，src/common/utils/utils_message.py）：
  seglist 里每段 {type, data}，text/image/emoji 的 data 是字符串（图片为 base64），
  at 的 data 可以是 {target_user_id, target_user_nickname, target_user_cardname}，reply 的 data 是被引用消息的 id。
"""

from __future__ import annotations

import base64
import json
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

from maim_message import BaseMessageInfo, FormatInfo, GroupInfo, MessageBase, Seg, UserInfo

from .feishu_api import FeishuAPI, FeishuAPIError

logger = logging.getLogger("feishu.inbound")

_MENTION_KEY_RE = re.compile(r"@_(?:user|all)_\d+|@_all")


@dataclass(frozen=True)
class BotIdentity:
    platform: str
    open_id: str
    name: str


@dataclass
class ParsedContent:
    segments: list[dict[str, Any]]
    plain_text: str
    mentioned_bot: bool


class RecentIds:
    """飞书长连接可能重投同一事件，按 message_id 去重。"""

    def __init__(self, capacity: int = 2048):
        self._capacity = capacity
        self._ids: "OrderedDict[str, None]" = OrderedDict()

    def seen(self, message_id: str) -> bool:
        if message_id in self._ids:
            return True
        self._ids[message_id] = None
        if len(self._ids) > self._capacity:
            self._ids.popitem(last=False)
        return False


def _mention_map(mentions: Any) -> dict[str, dict[str, str]]:
    """key(@_user_1) → {open_id, name}。兼容 SDK 对象与 dict。"""
    result: dict[str, dict[str, str]] = {}
    for mention in mentions or []:
        key = _attr(mention, "key")
        if not key:
            continue
        ident = _attr(mention, "id")
        open_id = _attr(ident, "open_id") if ident is not None else ""
        name = _attr(mention, "name") or ""
        result[str(key)] = {"open_id": str(open_id or ""), "name": str(name)}
    return result


def _attr(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _text_with_mentions(text: str, mentions: dict[str, dict[str, str]], bot: BotIdentity) -> ParsedContent:
    """把 "@_user_1 早" 拆成 [at, text] 段；命中机器人自己的 @ 记为 mentioned_bot。"""
    segments: list[dict[str, Any]] = []
    plain_parts: list[str] = []
    mentioned = False
    pos = 0
    for match in _MENTION_KEY_RE.finditer(text):
        key = match.group(0)
        info = mentions.get(key)
        if info is None:
            continue
        before = text[pos : match.start()]
        if before and segments and segments[-1]["type"] == "at" and before.startswith(" "):
            before = before[1:]
        if before:
            segments.append({"type": "text", "data": before})
            plain_parts.append(before)
        target_id = info["open_id"] or ("all" if key.startswith("@_all") else "")
        if target_id:
            if target_id == bot.open_id:
                mentioned = True
            segments.append(
                {
                    "type": "at",
                    "data": {
                        "target_user_id": target_id,
                        "target_user_nickname": info["name"] or target_id,
                        "target_user_cardname": info["name"] or None,
                    },
                }
            )
            plain_parts.append(f"@{info['name'] or target_id}")
        pos = match.end()
    rest = text[pos:]
    if rest:
        # 飞书文本里 @ 后面自带一个空格，MaiBot 渲染 @ 段时也会补空格，去掉一个避免双空格
        if segments and segments[-1]["type"] == "at" and rest.startswith(" "):
            rest = rest[1:]
        if rest:
            segments.append({"type": "text", "data": rest})
            plain_parts.append(rest)
    return ParsedContent(segments=segments, plain_text="".join(plain_parts).strip(), mentioned_bot=mentioned)


async def _image_segment(api: FeishuAPI, message_id: str, image_key: str) -> Optional[dict[str, Any]]:
    try:
        raw = await api.download_message_resource(message_id, image_key, "image")
    except FeishuAPIError as exc:
        logger.warning("下载图片失败 message=%s key=%s: %s", message_id, image_key, exc)
        return None
    return {"type": "image", "data": base64.b64encode(raw).decode("ascii")}


async def _parse_post(
    api: FeishuAPI, message_id: str, content: dict[str, Any], mentions: dict[str, dict[str, str]], bot: BotIdentity
) -> ParsedContent:
    """富文本：content 可能是 {"title","content":[[...]]} 或按语言包一层 {"zh_cn": {...}}。"""
    body = content
    if "content" not in body:
        for value in body.values():
            if isinstance(value, dict) and "content" in value:
                body = value
                break
    segments: list[dict[str, Any]] = []
    plain: list[str] = []
    mentioned = False
    title = str(body.get("title") or "").strip()
    if title:
        segments.append({"type": "text", "data": title + "\n"})
        plain.append(title)
    for line in body.get("content") or []:
        for node in line or []:
            tag = str(node.get("tag") or "")
            if tag in {"text", "a", "md"}:
                text = str(node.get("text") or "")
                if tag == "a" and node.get("href"):
                    text = f"{text}({node['href']})"
                if text:
                    parsed = _text_with_mentions(text, mentions, bot)
                    segments.extend(parsed.segments)
                    plain.append(parsed.plain_text)
                    mentioned = mentioned or parsed.mentioned_bot
            elif tag == "at":
                target = str(node.get("user_id") or "")
                name = str(node.get("user_name") or target)
                if target == bot.open_id:
                    mentioned = True
                segments.append(
                    {"type": "at", "data": {"target_user_id": target, "target_user_nickname": name, "target_user_cardname": name}}
                )
                plain.append(f"@{name}")
            elif tag == "img":
                seg = await _image_segment(api, message_id, str(node.get("image_key") or ""))
                if seg:
                    segments.append(seg)
                    plain.append("[图片]")
            elif tag == "emotion":
                emo = f"[{node.get('emoji_type', '表情')}]"
                segments.append({"type": "text", "data": emo})
                plain.append(emo)
            elif tag in {"media", "file"}:
                label = "[视频]" if tag == "media" else f"[文件:{node.get('file_name', '')}]"
                segments.append({"type": "text", "data": label})
                plain.append(label)
        segments.append({"type": "text", "data": "\n"})
    # 去掉末尾多余换行
    while segments and segments[-1] == {"type": "text", "data": "\n"}:
        segments.pop()
    return ParsedContent(segments=segments, plain_text="\n".join(p for p in plain if p).strip(), mentioned_bot=mentioned)


async def parse_content(
    api: FeishuAPI, message_id: str, message_type: str, raw_content: str, mentions_raw: Any, bot: BotIdentity
) -> ParsedContent:
    mentions = _mention_map(mentions_raw)
    try:
        content = json.loads(raw_content) if raw_content else {}
    except json.JSONDecodeError:
        content = {"text": raw_content}
    if not isinstance(content, dict):
        content = {}

    if message_type == "text":
        return _text_with_mentions(str(content.get("text") or ""), mentions, bot)
    if message_type == "post":
        return await _parse_post(api, message_id, content, mentions, bot)
    if message_type == "image":
        seg = await _image_segment(api, message_id, str(content.get("image_key") or ""))
        if seg:
            return ParsedContent(segments=[seg], plain_text="[图片]", mentioned_bot=False)
        return ParsedContent(segments=[{"type": "text", "data": "[图片]"}], plain_text="[图片]", mentioned_bot=False)

    placeholders = {
        "sticker": "[表情包]",
        "file": f"[文件:{content.get('file_name', '')}]",
        "audio": "[语音]",
        "media": "[视频]",
        "video": "[视频]",
        "merge_forward": "[合并转发消息]",
        "share_chat": "[群名片]",
        "share_user": "[个人名片]",
        "interactive": "[卡片消息]",
        "system": "[系统消息]",
        "location": "[位置]",
    }
    label = placeholders.get(message_type, f"[{message_type}]")
    return ParsedContent(segments=[{"type": "text", "data": label}], plain_text=label, mentioned_bot=False)


async def event_to_message(
    event: Any,
    api: FeishuAPI,
    bot: BotIdentity,
    chat_allowlist: frozenset[str],
    recent: RecentIds,
) -> Optional[MessageBase]:
    """把一条 P2ImMessageReceiveV1 转成 MessageBase；返回 None 表示这条不该转发。"""
    data = _attr(event, "event")
    sender = _attr(data, "sender")
    message = _attr(data, "message")
    if sender is None or message is None:
        return None

    sender_type = str(_attr(sender, "sender_type") or "")
    sender_id = _attr(sender, "sender_id")
    sender_open_id = str(_attr(sender_id, "open_id") or "") if sender_id is not None else ""
    if sender_type != "user" or not sender_open_id or sender_open_id == bot.open_id:
        return None

    message_id = str(_attr(message, "message_id") or "")
    if not message_id or recent.seen(message_id):
        return None

    chat_id = str(_attr(message, "chat_id") or "")
    chat_type = str(_attr(message, "chat_type") or "")  # "p2p" | "group"
    if chat_allowlist and chat_id not in chat_allowlist and chat_type not in chat_allowlist:
        logger.debug("跳过不在白名单的会话 %s", chat_id)
        return None

    parsed = await parse_content(
        api,
        message_id,
        str(_attr(message, "message_type") or ""),
        str(_attr(message, "content") or ""),
        _attr(message, "mentions"),
        bot,
    )
    segments = list(parsed.segments)

    parent_id = str(_attr(message, "parent_id") or "")
    if parent_id:
        segments.insert(0, {"type": "reply", "data": parent_id})

    if not segments:
        return None

    sender_name = await api.user_name(sender_open_id)
    user_info = UserInfo(platform=bot.platform, user_id=sender_open_id, user_nickname=sender_name, user_cardname=sender_name)

    group_info = None
    additional_config: dict[str, Any] = {
        "self_id": bot.open_id,
        "feishu": {"chat_id": chat_id, "chat_type": chat_type, "message_id": message_id},
    }
    if chat_type == "group":
        group_info = GroupInfo(platform=bot.platform, group_id=chat_id, group_name=await api.chat_name(chat_id))
        additional_config["platform_io_target_group_id"] = chat_id
    else:
        additional_config["platform_io_target_user_id"] = sender_open_id
    if parsed.mentioned_bot:
        additional_config["at_bot"] = True

    try:
        create_time = float(int(_attr(message, "create_time") or 0)) / 1000.0
    except (TypeError, ValueError):
        create_time = 0.0
    if create_time <= 0:
        import time

        create_time = time.time()

    message_info = BaseMessageInfo(
        platform=bot.platform,
        message_id=message_id,
        time=create_time,
        user_info=user_info,
        group_info=group_info,
        format_info=FormatInfo(
            content_format=["text", "image", "at", "reply"],
            accept_format=["text", "image", "emoji", "at", "reply"],
        ),
        template_info=None,
        additional_config=additional_config,
    )
    return MessageBase(
        message_info=message_info,
        message_segment=Seg(type="seglist", data=[Seg(type=s["type"], data=s["data"]) for s in segments]),
        raw_message=parsed.plain_text,
    )
