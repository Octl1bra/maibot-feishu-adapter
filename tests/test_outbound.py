import base64
import json

from maibot_feishu.outbound import deliver, plan_outbound
from tests.fakes import FakeFeishuAPI

BOT_ID = "ou_bot"


def maibot_outbound(*, group_id=None, target_user=None, segments=None):
    """按 MaiBot 1.2.5 to_maim_message() 的形态造一条出站消息。"""
    info = {
        "platform": "feishu",
        "message_id": "mm_1",
        "time": 1757900001.0,
        "user_info": {"platform": "feishu", "user_id": BOT_ID, "user_nickname": "麦麦", "user_cardname": None},
        "group_info": {"platform": "feishu", "group_id": group_id, "group_name": "测试群"} if group_id else None,
        "additional_config": {"platform_io_target_user_id": target_user} if target_user else {},
        "sender_info": None,
        "receiver_info": None,
    }
    return {"message_info": info, "message_segment": {"type": "seglist", "data": segments or []}}


async def test_group_text_with_at_and_reply():
    api = FakeFeishuAPI()
    msg = maibot_outbound(
        group_id="oc_g",
        segments=[
            {"type": "reply", "data": "om_q"},
            {"type": "at", "data": "ou_alice"},
            {"type": "text", "data": "早呀"},
        ],
    )
    plan = plan_outbound(msg, BOT_ID)
    assert plan.receive_id == "oc_g" and plan.receive_id_type == "chat_id"
    assert plan.reply_to == "om_q"
    assert plan.text == '<at user_id="ou_alice"></at> 早呀'
    ids = await deliver(plan, api)
    assert ids == ["om_reply_1"]
    assert api.replied[0]["message_id"] == "om_q"
    assert json.loads(api.replied[0]["content"]) == {"text": '<at user_id="ou_alice"></at> 早呀'}
    assert api.sent == []


async def test_private_uses_target_user_open_id():
    api = FakeFeishuAPI()
    msg = maibot_outbound(target_user="ou_alice", segments=[{"type": "text", "data": "私聊你"}])
    plan = plan_outbound(msg, BOT_ID)
    assert plan.receive_id == "ou_alice" and plan.receive_id_type == "open_id"
    await deliver(plan, api)
    assert api.sent[0]["receive_id_type"] == "open_id"


async def test_reply_failure_falls_back_to_send():
    api = FakeFeishuAPI()
    api.fail_reply = True
    msg = maibot_outbound(group_id="oc_g", segments=[{"type": "reply", "data": "om_gone"}, {"type": "text", "data": "x"}])
    ids = await deliver(plan_outbound(msg, BOT_ID), api)
    assert ids == ["om_sent_1"]


async def test_images_uploaded_and_sent_separately():
    api = FakeFeishuAPI()
    png = base64.b64encode(b"img-bytes").decode()
    msg = maibot_outbound(group_id="oc_g", segments=[{"type": "text", "data": "看图"}, {"type": "image", "data": png}, {"type": "emoji", "data": png}])
    ids = await deliver(plan_outbound(msg, BOT_ID), api)
    assert len(ids) == 3
    assert api.uploads == [b"img-bytes", b"img-bytes"]
    assert [s["msg_type"] for s in api.sent] == ["text", "image", "image"]
    assert json.loads(api.sent[1]["content"]) == {"image_key": "img_key_1"}


async def test_unsupported_segments_skipped_not_fatal():
    api = FakeFeishuAPI()
    msg = maibot_outbound(group_id="oc_g", segments=[{"type": "voice", "data": "AAAA"}, {"type": "text", "data": "还有字"}])
    plan = plan_outbound(msg, BOT_ID)
    assert plan.skipped == ["voice"]
    ids = await deliver(plan, api)
    assert len(ids) == 1


async def test_no_target_is_dropped():
    msg = maibot_outbound(segments=[{"type": "text", "data": "x"}])
    assert plan_outbound(msg, BOT_ID) is None


async def test_empty_plan_sends_nothing():
    api = FakeFeishuAPI()
    plan = plan_outbound(maibot_outbound(group_id="oc_g", segments=[{"type": "voice", "data": "AAAA"}]), BOT_ID)
    assert await deliver(plan, api) == []
    assert api.sent == []
