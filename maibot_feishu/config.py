"""全部配置来自环境变量，方便在 k8s 里用 Secret / env 注入，不落配置文件。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是整数，收到 {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    # 飞书应用（开放平台 → 凭证与基础信息）
    app_id: str
    app_secret: str
    encrypt_key: str = ""          # 长连接模式不需要，留空即可
    verification_token: str = ""   # 同上
    feishu_domain: str = "https://open.feishu.cn"

    # MaiBot 旧版 maim_message WebSocket 服务（bot_config.toml 的 [maim_message]）
    maibot_host: str = "127.0.0.1"
    maibot_port: int = 8000
    maibot_platform: str = "feishu"
    maibot_token: str = ""         # 对应 [maim_message].auth_token，MaiBot 侧为空时留空

    # 只转发这些会话（chat_id 或 "p2p"），空 = 全部转发
    chat_allowlist: frozenset[str] = field(default_factory=frozenset)

    health_port: int = 8765
    log_level: str = "INFO"

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret)

    @property
    def maibot_ws_url(self) -> str:
        return f"ws://{self.maibot_host}:{self.maibot_port}/ws"

    @classmethod
    def from_env(cls) -> "Settings":
        allowlist = frozenset(x.strip() for x in _env("FEISHU_CHAT_ALLOWLIST").split(",") if x.strip())
        return cls(
            app_id=_env("FEISHU_APP_ID"),
            app_secret=_env("FEISHU_APP_SECRET"),
            encrypt_key=_env("FEISHU_ENCRYPT_KEY"),
            verification_token=_env("FEISHU_VERIFICATION_TOKEN"),
            feishu_domain=_env("FEISHU_DOMAIN", "https://open.feishu.cn").rstrip("/"),
            maibot_host=_env("MAIBOT_HOST", "127.0.0.1"),
            maibot_port=_env_int("MAIBOT_PORT", 8000),
            maibot_platform=_env("MAIBOT_PLATFORM", "feishu").lower(),
            maibot_token=_env("MAIBOT_TOKEN"),
            chat_allowlist=allowlist,
            health_port=_env_int("HEALTH_PORT", 8765),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
        )


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # lark SDK 的 debug 日志非常吵
    logging.getLogger("Lark").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
