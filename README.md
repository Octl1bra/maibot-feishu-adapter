# maibot-feishu-adapter

把 [MaiBot（麦麦）](https://github.com/MaiM-with-u/MaiBot) 接进飞书。MaiBot 官方没有飞书适配器（2026-09-15 查过文档、
官方插件索引 370 个、Mai-with-u 组织 31 个仓库），这是自己写的桥。

```
飞书群/私聊 ──长连接推送──▶ 本进程 ──ws://maibot:8000/ws（旧版 maim_message）──▶ MaiBot
飞书群/私聊 ◀──REST 发消息── 本进程 ◀──────────── 同一条 WS 回包 ────────────── MaiBot
```

- 飞书侧：`lark-oapi` 的 WebSocket **长连接**订阅 `im.message.receive_v1`，不需要公网回调地址。
- MaiBot 侧：MaiBot 1.2.5 仍默认监听旧版 `maim_message` WebSocket（`bot_config.toml` 的 `[maim_message]`，默认 `127.0.0.1:8000`），
  本进程用 `maim_message==0.6.8`（与 MaiBot 锁文件同版本）的 `Router` 当客户端。
- **群里所有消息都转发**（不只 @），说不说话由 MaiBot 自己的规划器决定；被 @ 时会打上 `at_bot` 标记并附结构化 `at` 段，
  MaiBot 的 `is_mentioned_bot_in_message` 认这两种。
- 无状态。凭据缺失时进入空闲模式（健康检查仍返回 200，pod 不会因为它不 Ready）。

## 环境变量
| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 空 | 飞书企业自建应用凭据。缺任一 → 空闲模式 |
| `FEISHU_ENCRYPT_KEY` / `FEISHU_VERIFICATION_TOKEN` | 空 | 长连接模式不需要 |
| `FEISHU_DOMAIN` | `https://open.feishu.cn` | Lark 国际版改 `https://open.larksuite.com` |
| `MAIBOT_HOST` / `MAIBOT_PORT` | `127.0.0.1` / `8000` | MaiBot 旧版 WS；同 pod sidecar 用默认值 |
| `MAIBOT_PLATFORM` | `feishu` | 平台名，要和 MaiBot `[bot] platforms` 里的前缀一致 |
| `MAIBOT_TOKEN` | 空 | 对应 MaiBot `[maim_message] auth_token` |
| `FEISHU_CHAT_ALLOWLIST` | 空=全部 | 逗号分隔的 chat_id，或 `p2p` 表示放行所有私聊 |
| `HEALTH_PORT` | `8765` | `GET /healthz` |
| `LOG_LEVEL` | `INFO` | |

## MaiBot 侧要改的一处配置
`config/bot_config.toml`：
```toml
[bot]
platforms = ["feishu:ou_xxxxxxxx"]   # 机器人自己的 open_id，本进程启动日志会打印出来
```
旧链回包靠 `platforms` 建 `legacy.send.feishu` 驱动（`platform_io/manager.py::_sync_legacy_send_drivers` 只读配置，不读数据库身份）；
不配的话 MaiBot 收得到、回不出去。WebUI 里改也行。

## 飞书开放平台要做的事（需要有开放平台权限的人）
1. 创建**企业自建应用**，开启「机器人」能力。
2. 权限管理里开通（开完要**发布版本**才生效）：
   - `im:message`（收发消息）、`im:message.p2p_msg`（私聊）、`im:message.group_at_msg`（群 @）
   - **`im:message.group_msg`**（群内**所有**消息，敏感权限，需要管理员审批）——不开就只收得到 @ 和私聊，"像人"就没了
   - `im:resource`（收发图片）
   - 可选：`contact:user.base:readonly`（显示真实姓名，否则昵称退化成 `飞书用户+open_id 尾号`）、`im:chat:readonly`（群名）
3. 事件订阅：选**「使用长连接接收事件」**，添加 `im.message.receive_v1`。不需要回调 URL、Encrypt Key、Verification Token。
4. 把机器人拉进要玩的群。
5. `App ID` / `App Secret` 存 1Password，再进 k8s Secret（见 `../maibot.yaml`）。

## 消息映射
| 飞书 → MaiBot | 处理 |
| --- | --- |
| text | 按 mentions 拆成 `text` / `at` 段；@机器人 → `additional_config.at_bot=true` |
| post（富文本） | 标题+逐行展开：text/a/at/img(下载→base64)/emotion/media/file |
| image | 下载 → `image` 段（base64） |
| sticker/file/audio/media/merge_forward/… | 占位文本 `[表情包]` `[文件:x]` `[语音]` … |
| parent_id（引用回复） | 在最前插一段 `reply`，data=被引用的飞书 message_id |
| sender_type ≠ user / 机器人自己 / 重复 message_id | 丢弃 |

| MaiBot → 飞书 | 处理 |
| --- | --- |
| text + at | 合成一条 text 消息，`<at user_id="ou_x"></at>` |
| reply | 走 `messages/{id}/reply`；被引用消息没了就退化成普通发送 |
| image / emoji（base64） | `im/v1/images` 上传后各发一条 image 消息 |
| voice / dict / 其它 | 跳过并记日志 |
| 目标 | 群：`group_info.group_id`；私聊：`additional_config.platform_io_target_user_id` |

## 本地验证
```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q                        # 18 个单测：两个方向的转换
.venv/bin/python tests/roundtrip_fake_maibot.py      # 用 maim_message 自带 MessageServer 扮 MaiBot，过真 WebSocket 双向
.venv/bin/python tests/smoke_real_maibot.py 90       # 本机跑着真 MaiBot（8000）时：投两条群消息，等回包
```
真 MaiBot 本地跑法：clone 后 `uv sync --locked --no-dev --no-install-project`，
`EULA_AGREE=… PRIVACY_AGREE=… MAIBOT_LEGACY_0X_UPGRADE_CONFIRMED=1 MAIBOT_WORKER_PROCESS=1 .venv/bin/python bot.py`，
首启生成 `config/`，把 `[bot] platforms` 加上、`[telemetry] enable=false`、`model_config.toml` 的 base_url 指到 `scratchpad/stub_llm.py`。

## 已知限制 / 风险
- 依赖 MaiBot 的**旧版**协议（WebUI 里就叫"旧版 WS"）；哪天上游拆了，要改成插件 SDK 形态（参考 NapCat / Telegram 适配器）。
- 飞书长连接：每个应用最多 10 条并发连接；事件在多条连接间负载均衡，**同一个 app 别再开第二个消费者**（比如 `lark-cli event consume`），
  否则消息会被分走。
- 语音、文件、卡片只做占位文本；表情包（sticker）不下载。
- 图片走 base64 进 MaiBot，大图会胖；飞书单图上传上限 10MB。
