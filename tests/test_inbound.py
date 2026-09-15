import json

import pytest

from maibot_feishu.inbound import BotIdentity, RecentIds, event_to_message
from tests.fakes import FakeFeishuAPI, make_event, mention

BOT = BotIdentity(platform="feishu", open_id="ou_bot", name="麦麦")


def segs(message):
    return [(s.type, s.data) for s in message.message_segment.data]


async def test_group_text_message_basic():
    api = FakeFeishuAPI()
    msg = await event_to_message(make_event(), api, BOT, frozenset(), RecentIds())
    assert msg is not None
    info = msg.message_info
    assert info.platform == "feishu"
    assert info.message_id == "om_1"
    assert info.time == pytest.approx(1757900000.0)
    assert info.user_info.user_id == "ou_alice" and info.user_info.user_nickname == "Alice"
    assert info.group_info.group_id == "oc_group1" and info.group_info.group_name == "测试群"
    assert info.additional_config["self_id"] == "ou_bot"
    assert info.additional_config["platform_io_target_group_id"] == "oc_group1"
    assert "at_bot" not in info.additional_config
    assert segs(msg) == [("text", "hello")]
    assert msg.raw_message == "hello"
    d = msg.to_dict()
    assert d["message_segment"]["type"] == "seglist"


async def test_private_message_targets_sender():
    api = FakeFeishuAPI()
    msg = await event_to_message(make_event(chat_id="oc_p2p", chat_type="p2p"), api, BOT, frozenset(), RecentIds())
    assert msg.message_info.group_info is None
    assert msg.message_info.additional_config["platform_io_target_user_id"] == "ou_alice"


async def test_mentions_become_at_segments_and_flag_bot():
    api = FakeFeishuAPI()
    ev = make_event(
        content=json.dumps({"text": "@_user_1 早上好 @_user_2 你也是"}),
        mentions=[mention("@_user_1", "ou_bot", "麦麦"), mention("@_user_2", "ou_bob", "Bob")],
    )
    msg = await event_to_message(ev, api, BOT, frozenset(), RecentIds())
    kinds = [t for t, _ in segs(msg)]
    assert kinds == ["at", "text", "at", "text"]
    first_at = msg.message_segment.data[0].data
    assert first_at["target_user_id"] == "ou_bot" and first_at["target_user_nickname"] == "麦麦"
    assert msg.message_info.additional_config["at_bot"] is True
    assert msg.raw_message == "@麦麦早上好 @Bob你也是"


async def test_mention_other_user_does_not_flag_bot():
    api = FakeFeishuAPI()
    ev = make_event(content=json.dumps({"text": "@_user_1 hi"}), mentions=[mention("@_user_1", "ou_bob", "Bob")])
    msg = await event_to_message(ev, api, BOT, frozenset(), RecentIds())
    assert "at_bot" not in msg.message_info.additional_config


async def test_image_message_downloads_and_base64():
    api = FakeFeishuAPI()
    ev = make_event(message_type="image", content=json.dumps({"image_key": "img_v2_x"}))
    msg = await event_to_message(ev, api, BOT, frozenset(), RecentIds())
    assert api.downloads == [("om_1", "img_v2_x", "image")]
    (kind, data), = segs(msg)
    assert kind == "image"
    import base64

    assert base64.b64decode(data) == b"\x89PNGfake"
    assert msg.raw_message == "[图片]"


async def test_post_rich_text_with_image_and_at():
    api = FakeFeishuAPI()
    content = {
        "title": "标题",
        "content": [
            [{"tag": "text", "text": "第一行 "}, {"tag": "at", "user_id": "ou_bot", "user_name": "麦麦"}],
            [{"tag": "img", "image_key": "img_k"}],
        ],
    }
    ev = make_event(message_type="post", content=json.dumps(content))
    msg = await event_to_message(ev, api, BOT, frozenset(), RecentIds())
    kinds = [t for t, _ in segs(msg)]
    assert kinds == ["text", "text", "at", "text", "image"]
    assert msg.message_info.additional_config["at_bot"] is True
    assert "标题" in msg.raw_message and "@麦麦" in msg.raw_message


async def test_parent_id_becomes_reply_segment_first():
    api = FakeFeishuAPI()
    msg = await event_to_message(make_event(parent_id="om_parent"), api, BOT, frozenset(), RecentIds())
    assert segs(msg)[0] == ("reply", "om_parent")


async def test_skips_app_sender_and_self():
    api = FakeFeishuAPI()
    assert await event_to_message(make_event(sender_type="app"), api, BOT, frozenset(), RecentIds()) is None
    assert await event_to_message(make_event(sender_open_id="ou_bot"), api, BOT, frozenset(), RecentIds()) is None


async def test_dedup_by_message_id():
    api = FakeFeishuAPI()
    recent = RecentIds()
    assert await event_to_message(make_event(), api, BOT, frozenset(), recent) is not None
    assert await event_to_message(make_event(), api, BOT, frozenset(), recent) is None


async def test_allowlist_filters_chats():
    api = FakeFeishuAPI()
    allow = frozenset({"oc_other"})
    assert await event_to_message(make_event(), api, BOT, allow, RecentIds()) is None
    allow_p2p = frozenset({"p2p"})
    assert await event_to_message(make_event(chat_type="p2p", chat_id="oc_x"), api, BOT, allow_p2p, RecentIds()) is not None


async def test_unknown_types_become_placeholders():
    api = FakeFeishuAPI()
    ev = make_event(message_type="sticker", content=json.dumps({"file_key": "k"}))
    msg = await event_to_message(ev, api, BOT, frozenset(), RecentIds())
    assert segs(msg) == [("text", "[表情包]")]
