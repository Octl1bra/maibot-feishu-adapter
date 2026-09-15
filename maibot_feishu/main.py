"""进程入口：python -m maibot_feishu"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

import aiohttp

from . import __version__
from .config import Settings, setup_logging
from .feishu_api import FeishuAPI, FeishuAPIError
from .feishu_link import FeishuLink
from .health import start_health_server
from .inbound import BotIdentity, RecentIds, event_to_message
from .maibot_link import MaiBotLink
from .outbound import deliver, plan_outbound

logger = logging.getLogger("bridge")


class Bridge:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._session: aiohttp.ClientSession | None = None
        self._api: FeishuAPI | None = None
        self._bot: BotIdentity | None = None
        self._maibot: MaiBotLink | None = None
        self._feishu: FeishuLink | None = None
        self._recent = RecentIds()
        self._stats = {"inbound": 0, "outbound": 0, "dropped": 0, "errors": 0}

    # ---------- 状态 ----------
    def status(self) -> dict[str, Any]:
        if not self._settings.configured:
            return {"ok": True, "mode": "idle", "reason": "FEISHU_APP_ID/FEISHU_APP_SECRET 未配置"}
        maibot_ok = self._maibot is not None and self._maibot.connected
        feishu_ok = self._feishu is not None and self._feishu.alive
        return {
            "ok": maibot_ok and feishu_ok,
            "mode": "bridge",
            "version": __version__,
            "maibot_connected": maibot_ok,
            "feishu_connected": feishu_ok,
            "bot_open_id": self._bot.open_id if self._bot else "",
            **self._stats,
        }

    # ---------- 生命周期 ----------
    async def _resolve_bot_identity(self) -> BotIdentity:
        assert self._api is not None
        delay = 5
        while True:
            try:
                info = await self._api.bot_info()
                if info["open_id"]:
                    return BotIdentity(platform=self._settings.maibot_platform, open_id=info["open_id"], name=info["app_name"])
                logger.error("bot/v3/info 没返回 open_id：应用是否开启了「机器人」能力？返回=%s", info)
            except (FeishuAPIError, aiohttp.ClientError) as exc:
                logger.error("获取机器人身份失败（app_id/secret 对吗？）: %s", exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)

    async def run(self) -> None:
        settings = self._settings
        health = await start_health_server(settings.health_port, self.status)
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        if not settings.configured:
            logger.warning("未配置飞书应用凭据，进入空闲模式（健康检查返回 idle）。配好 Secret 后重启即可。")
            await stop_event.wait()
            await health.cleanup()
            return

        self._session = aiohttp.ClientSession()
        self._api = FeishuAPI(self._session, settings.app_id, settings.app_secret, settings.feishu_domain)
        try:
            self._bot = await self._resolve_bot_identity()
            logger.info("机器人身份: %s (%s)", self._bot.name, self._bot.open_id)
            logger.info(
                '请确认 MaiBot 的 config/bot_config.toml 里有：[bot] platforms = ["%s:%s"]（旧链回包靠它路由）',
                self._bot.platform,
                self._bot.open_id,
            )

            self._maibot = MaiBotLink(settings.maibot_platform, settings.maibot_ws_url, settings.maibot_token, self._on_outbound)
            self._maibot.start()
            self._feishu = FeishuLink(
                settings.app_id,
                settings.app_secret,
                settings.encrypt_key,
                settings.verification_token,
                settings.feishu_domain,
                loop,
                self._on_feishu_event,
            )
            self._feishu.start()

            await stop_event.wait()
            logger.info("收到停止信号，关闭中")
        finally:
            if self._maibot:
                await self._maibot.stop()
            await self._session.close()
            await health.cleanup()

    # ---------- 两个方向 ----------
    async def _on_feishu_event(self, event: Any) -> None:
        assert self._api is not None and self._bot is not None and self._maibot is not None
        try:
            message = await event_to_message(event, self._api, self._bot, self._settings.chat_allowlist, self._recent)
        except Exception:
            self._stats["errors"] += 1
            logger.exception("转换飞书消息失败")
            return
        if message is None:
            self._stats["dropped"] += 1
            return
        info = message.message_info
        where = info.group_info.group_name if info.group_info else "私聊"
        logger.info("飞书→MaiBot [%s] %s: %s", where, info.user_info.user_nickname, (message.raw_message or "")[:60])
        try:
            await self._maibot.send(message)
            self._stats["inbound"] += 1
        except Exception:
            self._stats["errors"] += 1
            logger.exception("投递到 MaiBot 失败（连接是否已建立？）")

    async def _on_outbound(self, message: dict[str, Any]) -> None:
        assert self._api is not None and self._bot is not None
        plan = plan_outbound(message, self._bot.open_id)
        if plan is None:
            self._stats["dropped"] += 1
            return
        logger.info(
            "MaiBot→飞书 [%s %s] %s%s",
            plan.receive_id_type,
            plan.receive_id,
            plan.text[:60],
            f" +{len(plan.images)}图" if plan.images else "",
        )
        try:
            sent = await deliver(plan, self._api)
            self._stats["outbound"] += len(sent)
        except (FeishuAPIError, aiohttp.ClientError):
            self._stats["errors"] += 1
            logger.exception("发送到飞书失败")


def main() -> None:
    settings = Settings.from_env()
    setup_logging(settings.log_level)
    logger.info("maibot-feishu-adapter %s 启动，MaiBot=%s platform=%s", __version__, settings.maibot_ws_url, settings.maibot_platform)
    asyncio.run(Bridge(settings).run())


if __name__ == "__main__":
    main()
