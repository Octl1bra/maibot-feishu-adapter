"""飞书开放平台 REST 调用（aiohttp，全异步）。只封装桥接用得到的几个接口。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import aiohttp

logger = logging.getLogger("feishu.api")

_TOKEN_EXPIRED_CODES = {99991661, 99991663, 99991668}  # tenant_access_token 失效/不合法


class FeishuAPIError(Exception):
    def __init__(self, code: int, msg: str, path: str):
        super().__init__(f"{path} -> code={code} msg={msg}")
        self.code = code
        self.msg = msg
        self.path = path


class _TTLCache:
    def __init__(self, ttl_seconds: float, max_size: int = 4096):
        self._ttl = ttl_seconds
        self._max = max_size
        self._data: dict[str, tuple[float, str]] = {}

    def get(self, key: str) -> Optional[str]:
        item = self._data.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def put(self, key: str, value: str) -> None:
        if len(self._data) >= self._max:
            self._data.pop(next(iter(self._data)))
        self._data[key] = (time.monotonic() + self._ttl, value)


class FeishuAPI:
    def __init__(self, session: aiohttp.ClientSession, app_id: str, app_secret: str, domain: str):
        self._session = session
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = domain.rstrip("/")
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._user_names = _TTLCache(ttl_seconds=3600)
        self._chat_names = _TTLCache(ttl_seconds=3600)

    # ---------- 鉴权 ----------
    async def tenant_token(self, force_refresh: bool = False) -> str:
        async with self._token_lock:
            if not force_refresh and self._token and time.monotonic() < self._token_expires_at:
                return self._token
            async with self._session.post(
                f"{self._domain}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                body = await resp.json(content_type=None)
            if body.get("code") != 0:
                raise FeishuAPIError(body.get("code", -1), body.get("msg", ""), "tenant_access_token")
            self._token = body["tenant_access_token"]
            # 官方 2 小时；提前 5 分钟换
            self._token_expires_at = time.monotonic() + max(int(body.get("expire", 7200)) - 300, 60)
            return self._token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json: Optional[dict[str, Any]] = None,
        data: Any = None,
        raw: bool = False,
        _retry: bool = True,
    ) -> Any:
        token = await self.tenant_token()
        headers = {"Authorization": f"Bearer {token}"}
        async with self._session.request(
            method,
            f"{self._domain}/open-apis{path}",
            params=params,
            json=json,
            data=data,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if raw and "application/json" not in content_type:
                if resp.status != 200:
                    raise FeishuAPIError(resp.status, await resp.text(), path)
                return await resp.read()
            body = await resp.json(content_type=None)
        code = body.get("code", -1)
        if code == 0:
            # 绝大多数接口把结果包在 data 里；bot/v3/info 这类老接口直接平铺在顶层（{"code":0,"bot":{...}}）
            if "data" in body:
                return body.get("data") or {}
            return {k: v for k, v in body.items() if k not in ("code", "msg")}
        if code in _TOKEN_EXPIRED_CODES and _retry:
            await self.tenant_token(force_refresh=True)
            return await self._request(method, path, params=params, json=json, data=data, raw=raw, _retry=False)
        raise FeishuAPIError(code, body.get("msg", ""), path)

    # ---------- 身份 ----------
    async def bot_info(self) -> dict[str, str]:
        data = await self._request("GET", "/bot/v3/info")
        bot = data.get("bot", data) if isinstance(data, dict) else {}
        return {"open_id": str(bot.get("open_id", "")), "app_name": str(bot.get("app_name", ""))}

    async def user_name(self, open_id: str) -> str:
        """需要 contact:user.base:readonly；没权限就退化成 open_id 尾号，不阻塞消息。"""
        cached = self._user_names.get(open_id)
        if cached:
            return cached
        name = ""
        try:
            data = await self._request("GET", f"/contact/v3/users/{open_id}", params={"user_id_type": "open_id"})
            user = data.get("user", {})
            name = str(user.get("name") or user.get("nickname") or "").strip()
        except (FeishuAPIError, aiohttp.ClientError) as exc:
            logger.debug("查用户名失败 %s: %s", open_id, exc)
        if not name:
            name = f"飞书用户{open_id[-6:]}"
        self._user_names.put(open_id, name)
        return name

    async def chat_name(self, chat_id: str) -> str:
        """需要 im:chat:readonly（应用需在群内）；失败退化成 chat_id 尾号。"""
        cached = self._chat_names.get(chat_id)
        if cached:
            return cached
        name = ""
        try:
            data = await self._request("GET", f"/im/v1/chats/{chat_id}")
            name = str(data.get("name") or "").strip()
        except (FeishuAPIError, aiohttp.ClientError) as exc:
            logger.debug("查群名失败 %s: %s", chat_id, exc)
        if not name:
            name = f"飞书群{chat_id[-6:]}"
        self._chat_names.put(chat_id, name)
        return name

    # ---------- 资源 ----------
    async def download_message_resource(self, message_id: str, file_key: str, resource_type: str = "image") -> bytes:
        return await self._request(
            "GET", f"/im/v1/messages/{message_id}/resources/{file_key}", params={"type": resource_type}, raw=True
        )

    async def upload_image(self, image_bytes: bytes) -> str:
        form = aiohttp.FormData()
        form.add_field("image_type", "message")
        form.add_field("image", image_bytes, filename="image", content_type="application/octet-stream")
        data = await self._request("POST", "/im/v1/images", data=form)
        return str(data["image_key"])

    # ---------- 发消息 ----------
    async def send_message(self, receive_id: str, receive_id_type: str, msg_type: str, content: str) -> str:
        data = await self._request(
            "POST",
            "/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            json={"receive_id": receive_id, "msg_type": msg_type, "content": content},
        )
        return str(data.get("message_id", ""))

    async def reply_message(self, message_id: str, msg_type: str, content: str) -> str:
        data = await self._request(
            "POST", f"/im/v1/messages/{message_id}/reply", json={"msg_type": msg_type, "content": content}
        )
        return str(data.get("message_id", ""))
