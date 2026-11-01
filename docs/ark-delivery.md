# Ark 回执与单聊回复凭证

`send.custom("qq_ark", payload, stream_id, return_details=True)` 复用正常消息网关、映射及发送管线，不提供任意 HTTP 请求能力。

## 区分拒绝和未知

部分 Host 版本的 `send.custom` 返回值仅保留 `sent=false`，不保留网关的 QQ 错误原因。适配器提供可选只读 API：

```python
await ctx.api.call("Mai-with-u.qq-adapter.ark_delivery_status", version="1",
    target_id="TARGET_USER_ID", is_group=False, ark=payload, after=attempt_started_at)
```

按目标、完整卡片内容和时间匹配最近回执；API 本身不发送或重试。明确 HTTP 4xx 拒绝为 `failed`，返回 `error_code/error`；网络超时、5xx、响应缺消息 ID、没有匹配记录均为 `unknown`。成功为 `sent`，含平台消息 ID。

回执只在适配器内存保留最近 128 条，重启后可能缺失。调用方应持久化自己的发送尝试与结果。没查到回执不等于未发送，不应自动重试。混合消息的回执针对 Ark 分片，不表示前后其他分片均成功。

## 单聊兼容选项

```toml
[qq_official]
ark_private_use_passive_reply = true
```

默认 true，维持原有行为。2026-11-01 对 `api.bot.qq.com` 一个现网账号的受控对照测试发现：同一 Ark 37 内容，携带 `msg_id/msg_seq` 返回 HTTP 400、`40034126`（无 ark 模板权限）；不携带这些字段返回 HTTP 200 和消息 ID。这说明该账号的此错误不能直接等同于模板 37 在所有发送方式下均不可用。

**只有核实符合上述情况后**，可将该项设为 false：仅单聊 Ark 不附带被动回复凭证，也不消耗本地被动回复序号；文字、图片、群聊 Ark 保持原有逻辑。仍受 QQ 对该账号的发送策略限制，不保证其他账号、其他场景同样可用。

本选项不会在报错后重发，不是自动切换发送方式的回退逻辑。没有权限或结果未知时应报告真实结果。实际显示使用腾讯 [Ark 37 大图模板](https://github.com/tencent-connect/bot-docs/blob/main/docs/develop/api-v2/server-inter/message/type/template/template_37.md)，不等于 B 站原生小程序卡片。
