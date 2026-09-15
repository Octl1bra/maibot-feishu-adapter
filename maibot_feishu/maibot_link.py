"""与 MaiBot 旧版 maim_message WebSocket 服务的连接（客户端角色，MaiBot 是服务端）。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from maim_message import MessageBase, RouteConfig, Router, TargetConfig

logger = logging.getLogger("maibot.link")

OutboundHandler = Callable[[dict[str, Any]], Awaitable[None]]


class MaiBotLink:
    def __init__(self, platform: str, ws_url: str, token: str, on_outbound: OutboundHandler):
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
