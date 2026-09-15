"""与 MaiBot 旧版 maim_message WebSocket 服务的连接（客户端角色，MaiBot 是服务端）。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

import aiohttp
from maim_message import MessageBase, RouteConfig, Router, TargetConfig

logger = logging.getLogger("maibot.link")

_patched = False


def _patch_aiohttp_ws_client() -> None:
    """两处补丁，都是冲着 maim_message 0.6.8 的 WebSocket 客户端去的：

    1. 它硬编码 compress=15 向 MaiBot 协商 permessage-deflate。本机回环上压缩毫无意义，而且线上出现过
       MaiBot 回包那一刻客户端报 WSMsgType.ERROR 然后掉线、回包丢失（2026-09-15）。对 MaiBot 的连接一律关掉压缩。
    2. 它遇到 ERROR 帧只打印 ws.exception()，而 aiohttp 对协议层错误（WebSocketError）并不设置 exception()，
       于是日志永远是 "连接错误: None"。这里把 ERROR 帧的 data 记下来，下次出事能看到真正原因。
    """

    global _patched
    if _patched:
        return
    _patched = True

    original_ws_connect = aiohttp.ClientSession.ws_connect

    def ws_connect(self, url, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(url, str) and url.endswith("/ws") and "compress" in kwargs:
            kwargs["compress"] = 0
        return original_ws_connect(self, url, **kwargs)

    aiohttp.ClientSession.ws_connect = ws_connect  # type: ignore[method-assign]

    original_receive = aiohttp.ClientWebSocketResponse.receive

    async def receive(self, timeout=None):  # type: ignore[no-untyped-def]
        msg = await original_receive(self, timeout)
        if msg.type is aiohttp.WSMsgType.ERROR:
            logger.error("MaiBot WebSocket 收到 ERROR 帧: data=%r close_code=%r", msg.data, self.close_code)
        elif msg.type is aiohttp.WSMsgType.CLOSE:
            logger.warning("MaiBot WebSocket 收到 CLOSE 帧: code=%r reason=%r", msg.data, msg.extra)
        return msg

    aiohttp.ClientWebSocketResponse.receive = receive  # type: ignore[method-assign]

OutboundHandler = Callable[[dict[str, Any]], Awaitable[None]]


class MaiBotLink:
    def __init__(self, platform: str, ws_url: str, token: str, on_outbound: OutboundHandler):
        _patch_aiohttp_ws_client()
        self._platform = platform
        self._ws_url = ws_url
        self._on_outbound = on_outbound
        target = TargetConfig(url=ws_url, token=token or None)
        self._router = Router(RouteConfig({platform: target}), custom_logger=logging.getLogger("maim_message"))
        self._router.register_class_handler(self._handle)
        self._task: Optional[asyncio.Task[None]] = None

    async def _handle(self, message: Any) -> None:
        if not isinstance(message, dict):
            logger.warning("收到非 dict 的 MaiBot 消息，忽略: %r", type(message))
            return
        platform = ((message.get("message_info") or {}).get("platform") or "").lower()
        if platform and platform != self._platform:
            return
        try:
            await self._on_outbound(message)
        except Exception:  # 不能让一条坏消息把回调链路带崩
            logger.exception("处理 MaiBot 出站消息失败")

    def start(self) -> None:
        logger.info("连接 MaiBot: %s (platform=%s)", self._ws_url, self._platform)
        self._task = asyncio.create_task(self._router.run(), name="maim_message.router")

    @property
    def connected(self) -> bool:
        return self._router.check_connection(self._platform)

    async def send(self, message: MessageBase) -> None:
        await self._router.send_message(message)

    async def stop(self) -> None:
        await self._router.stop()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
