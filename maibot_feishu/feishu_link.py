"""飞书事件长连接。

lark-oapi 的 ws.Client.start() 是阻塞的，而且内部用的是模块级 event loop（导入时 get_event_loop() 抓到的那个）。
所以放到独立线程里跑：线程先建自己的 loop 并 set_event_loop，再导入 SDK，让它抓到这个线程的 loop；
事件回调用 run_coroutine_threadsafe 投回主 loop。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("feishu.link")

EventHandler = Callable[[Any], Awaitable[None]]


class FeishuLink:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        encrypt_key: str,
        verification_token: str,
        domain: str,
        main_loop: asyncio.AbstractEventLoop,
        on_event: EventHandler,
    ):
        self._app_id = app_id
        self._app_secret = app_secret
        self._encrypt_key = encrypt_key
        self._verification_token = verification_token
        self._domain = domain
        self._main_loop = main_loop
        self._on_event = on_event
        self._thread: Optional[threading.Thread] = None
        self._failed = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="feishu-ws", daemon=True)
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._failed.is_set()

    def _dispatch(self, data: Any) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self._on_event(data), self._main_loop)
        except RuntimeError as exc:  # 主 loop 已关
            logger.warning("主事件循环不可用，丢弃事件: %s", exc)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            import lark_oapi.ws.client as ws_client_module
            from lark_oapi import ws
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

            # SDK 在导入时抓过一次 loop；如果之前已经被别处导入，这里强制指到本线程的 loop
            if hasattr(ws_client_module, "loop"):
                ws_client_module.loop = loop

            handler = (
                EventDispatcherHandler.builder(self._encrypt_key, self._verification_token)
                .register_p2_im_message_receive_v1(self._dispatch)
                .build()
            )
            client = ws.Client(
                app_id=self._app_id,
                app_secret=self._app_secret,
                event_handler=handler,
                domain=self._domain,
                auto_reconnect=True,
            )
            logger.info("建立飞书长连接 (app_id=%s)", self._app_id)
            client.start()  # 阻塞；SDK 自己处理心跳与重连
            logger.error("飞书长连接线程退出了")
        except Exception:
            logger.exception("飞书长连接线程崩溃")
        finally:
            self._failed.set()
            loop.close()
