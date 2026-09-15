import pytest

from maibot_feishu.feishu_api import FeishuAPI, FeishuAPIError


class _Resp:
    def __init__(self, body, status=200, content_type="application/json"):
        self._body = body; self.status = status; self.headers = {"Content-Type": content_type}
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def json(self, content_type=None): return self._body
    async def read(self): return self._body
    async def text(self): return str(self._body)


class _Session:
    def __init__(self, routes): self.routes = routes; self.calls = []
    def post(self, url, **kw):
        return _Resp({"code": 0, "tenant_access_token": "t-1", "expire": 7200})
    def request(self, method, url, **kw):
        self.calls.append((method, url))
        return _Resp(self.routes[url.split("/open-apis")[1].split("?")[0]])


async def test_bot_info_reads_top_level_bot_field():
    # bot/v3/info 不包 data：{"code":0,"msg":"ok","bot":{...}}
    api = FeishuAPI(_Session({"/bot/v3/info": {"code": 0, "msg": "ok", "bot": {"open_id": "ou_bot", "app_name": "贾伯斯"}}}), "cli", "sec", "https://x")
    assert await api.bot_info() == {"open_id": "ou_bot", "app_name": "贾伯斯"}


async def test_data_envelope_still_unwrapped():
    api = FeishuAPI(_Session({"/im/v1/chats/oc_1": {"code": 0, "data": {"name": "测试群"}}}), "cli", "sec", "https://x")
    assert await api.chat_name("oc_1") == "测试群"


async def test_error_code_raises():
    api = FeishuAPI(_Session({"/im/v1/chats/oc_2": {"code": 99991672, "msg": "no scope"}}), "cli", "sec", "https://x")
    assert (await api.chat_name("oc_2")).startswith("飞书群")   # chat_name 内部吞掉错误退化
    with pytest.raises(FeishuAPIError):
        await api.download_message_resource("om", "key")
