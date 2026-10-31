# MaiBot QQ 官方机器人适配器

直连 [QQ 开放平台](https://q.qq.com/)的 麦麦 qq 适配器

## 部署

需要 MaiBot >= 1.3.18；禁言和撤回工具仅在 QQ 平台提供。

1. 在 [QQ 开放平台](https://q.qq.com/)完成开发者入驻并创建机器人，在控制台取得 **AppID** 与 **AppSecret（ClientSecret）**。在控制台为机器人开通群聊/单聊相关能力。QQ 的 OpenID 不是普通 QQ 号。
2. 在 WebUI 的「QQ 官方机器人适配器 → QQ 开放平台」配置中填写 **AppID** 和 **AppSecret**。
3. 在已配置的测试群 **@机器人 发一条纯文本**，或由测试用户发起单聊并发送纯文本，观察 MaiBot 是否创建聊天流并回复。到 WebUI 聊天页的「适配器策略」检查 `qq` 平台是否被放行`。

配置示例（`plugins/qq_official_adapter/config.toml`）：

```toml
[plugin]
enabled = true
config_version = "0.4.2"

[qq_official]
app_id = "你的 AppID"
app_secret = "你的 AppSecret"
api_base_url = "https://api.bot.qq.com"
# 可选：统一账号 ID（如 NapCat 机器人 QQ 号）。填写后聊天流按该 ID 归属，
# 与同一 platform 下的其它适配器（如 SnowLuma/NapCat）共享上下文。
unified_account_id = ""
# 可选：允许执行绑定命令的用户（统一 ID 或 OpenID）；留空不限制。
assign_admin_ids = []

[mute]
enabled = true
# 留空时允许所有群使用禁言工具。
allowed_groups = []

[recall]
enabled = true
# 留空时允许所有群使用撤回工具，单聊不受群白名单限制。
allowed_groups = []

[advanced]
# 断线重连间隔（秒）。
reconnect_delay_sec = 5.0
```

## 出站消息

- 文本、@（含 @ 时以 markdown 发送以渲染真实 @）、图片与表情（jpg/png/gif/webp/bmp）。
- 官方一条消息只能承载一种内容，混合消息按原顺序拆成多条发送；每条各占一个被动回复序号（群聊每条入站消息最多被动回复 5 次）。
- 图片/表情走官方分片上传（`upload_prepare` → 分片 PUT → `upload_part_finish` → `files` 合并）取得 `file_info`，再以 `msg_type=7` 发送。
- 语音、文件出站暂不支持，会直接返回发送失败。

## 扫码登录

启用适配器后，在 WebUI「适配器管理 → QQ 官方机器人适配器」配置页点击「生成 / 刷新二维码」，使用手机 QQ 扫码并在官方页面完成机器人授权。成功后会将 AppID / AppSecret 原子保存到本插件的 `config.toml`，自动启用并连接；页面显示 AppID、机器人名称和实际连接状态，不显示 AppSecret。已有机器人凭据会被替换，请确认扫码授权的是目标机器人。

支持取消扫码、过期后重新生成二维码；二维码最多等待 5 分钟，官方提前返回过期时也会停止。关闭页面不会取消已经发起的授权，可重新打开查看进度。保存阶段会完成写入及连接切换后再结束，避免出现已取消但凭据仍被替换的情况。原有手动配置方式继续可用。

协议参考腾讯官方 [OpenClaw QQBot 登录实现](https://github.com/tencent-connect/openclaw-qqbot/blob/89988812442583bf47c34b89f9abf5fd4b85df0b/src/setup/login.ts)及其 `@tencent-connect/qqbot-connector@1.2.0`：创建绑定任务、轮询结果、使用 AES-256-GCM 解密凭据；二维码在本地生成。插件依赖由 `_manifest.json` 声明，无需 Node.js。

## 查看及绑定 ID 映射

WebUI 侧边栏「插件扩展 → QQ 群与用户映射」分别展示群 OpenID 对应的群号、用户 OpenID 对应的 QQ 号，以及头像、类型图标和绑定数量；执行绑定命令后点击刷新即可更新。页面为只读，无绑定时显示命令提示。头像按需加载，失败时显示原因；非数字绑定 ID 无法获取 QQ 头像。

QQ 官方平台的 OpenID 与真实 QQ 号不互通。为了让消息以真实 QQ 号归属（与 NapCat 系适配器的数据统一），可在聊天内发送绑定命令：

- `/assign_group_id <群号>`：把当前群的 group_openid 绑定到指定群号，群聊可用。
- `/assign_id <QQ号>`：把发送者的 OpenID 绑定到指定 QQ 号；`/assign_id @某人 <QQ号>` 可为被 @ 的用户绑定。

绑定立即生效并持久化到 `data/plugins/<插件ID>/id_map.json`：入站消息的群/用户 ID 按映射替换（原始 OpenID 保留在消息路由信息中），出站发送时自动反查回 OpenID 调用官方 API。命令消息不进入聊天流，绑定结果以回复回执。

注意：session 归属按 `platform + 账号 ID + 群/用户 ID` 计算。只做群/用户绑定时，ID 数据（统计、表达学习等）可与其它适配器对齐，但聊天流仍按本适配器的账号 ID 区分；要完全合并聊天流，在配置文件中填写 `unified_account_id` 指定同一账号 ID——此时该聊天流的出站消息会按路由精确匹配优先发往另一适配器。

## 禁言工具

提供 `mute` LLM 工具（参数：`msg_id`、`duration`、`reason`；按消息 ID 禁言发送者，所在群从该消息解析，调用官方 `POST /v2/groups/{group_openid}/restrict_chat_setting` 接口）。由配置 `[mute].enabled` 开关（默认开启，WebUI 插件配置「禁言工具」中可切换，保存即生效）；启用后受以下配置约束：

- `allowed_groups`：允许使用禁言的群（统一群号或 group_openid）；留空不限制
- `admin_users`：保护名单（统一 QQ 号或 OpenID），名单内用户不会被禁言
- `min_duration`/`max_duration`：时长限制（秒），官方上限 30 天，接口限频 60 QPM

平台限制：只能禁言普通成员（群主、管理员、机器人不可被禁言）；目标用户需能解析出 OpenID——其消息经本适配器接收时自动携带，否则需先 `/assign_id` 绑定。

## 撤回工具

提供 `recall_message` LLM 工具（参数：`msg_id`；所在群/单聊从该消息解析，调用官方 `DELETE /v2/groups/{group_openid}/messages/{message_id}` 或 `DELETE /v2/users/{user_openid}/messages/{message_id}` 接口）。由配置 `[recall].enabled` 开关（默认开启）；`allowed_groups` 限制可撤回的群（统一群号或 group_openid），留空不限制。

未填写工具配置或 `allowed_groups` 时，工具默认启用且不限制群；已有配置中显式设置的 `enabled = false` 仍会关闭对应工具。

平台限制：发送超过 2 分钟的消息不可撤回；群聊中机器人是群管理员时可撤回自己与普通群成员的消息，否则只能撤回自己发的消息；单聊只能撤回机器人自己发的消息。

## 头像接口

提供公开 API `adapter.avatar.get`（版本 `1`），与 SnowLuma 适配器使用相同的头像协议。参数为 `platform`、`target_id`、`target_type`（`user` 或 `group`，默认 `user`），并接受宿主传入的 `account_id` 和 `scope`。

- 数字 QQ 号、群号返回头像 URL 和 `expires_in = 86400`，图片由宿主统一下载、缓存。
- OpenID 已通过 `/assign_id` 或 `/assign_group_id` 绑定真实数字 ID 时，使用绑定值查询头像。
- 未绑定的 OpenID、非数字绑定值及非 `qq` 平台返回 `status = "unsupported"`。

## 戳一戳

QQ 开放平台官方 API 目前没有提供戳一戳接口（截至 2026-09-16 的官方变更记录），本适配器暂不支持。

## 引用消息

适配器会保留引用正文，并把能够精确关联的原消息 ID 传给主程序。索引缓存按机器人账号和群聊／私聊隔离，同时记录入站消息与机器人发送成功的回执。

缓存位于插件数据目录的 `reference_index.sqlite3`，包含消息正文、作者和引用索引，最多保留30天、100000个索引别名。备份或分享数据目录时请注意这些聊天数据。`advanced.reference_cache_enabled=false` 可关闭后续缓存与查询，已有文件不会自动删除。

腾讯有时会返回无法对应原消息的引用索引。默认情况下，适配器只保留引用正文并显示作者无法确认，不会把附近发言者当成原作者。

如需回退，可开启 `advanced.reference_history_fallback`（默认关闭）：在同一会话已收录的消息中匹配完整原文。只有匹配结果来自同一位作者时才给出作者候选，并标明“历史匹配，非平台确认”；多人同文时不认定作者。这不会创建原消息 ID 或修正腾讯的索引，也不能保证所有引用都能识别。

历史匹配不解析合并转发和嵌套记录，不下载引用中的附件。机器人尚未收到或缓存已过期的原消息无法补回。

## 参考致谢

- [qq-official-adapter](https://github.com/WhiteCloudOL/qq-official-adapter)（作者：[清蒸云鸭](https://github.com/WhiteCloudOL)）：为本适配器开发提供了参考和借鉴。


## 配置页运行时控件

需要支持运行时控件的新版主程序及 SDK >= 2.19.0。

启用适配器后，在配置页点击“生成 / 刷新二维码”，使用手机 QQ 授权。配置页同时显示连接状态，并可取消扫码或重新连接。扫码成功后 AppID / AppSecret 沿用原有配置保存流程；二维码、临时密钥和实时状态不写入 TOML。群与用户映射扩展页继续保留。

本地开发可设置 `MAIBOT_PLUGIN_SDK_PATH` 指向新版 SDK 仓库，然后重启主程序。

