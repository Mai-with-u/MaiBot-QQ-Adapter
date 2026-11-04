import asyncio
import time

import pytest

from qq_official_adapter.plugin import PROACTIVE_MSG_SEQ_RANGE, QQOfficialAdapterPlugin

GROUP_OPENID = "group-openid"
USER_OPENID = "user-openid"
REPLY_ID = "ROBOT1.0_reply"


class _Logger:
    def __init__(self) -> None:
        self.records = []

    def debug(self, message, *args, **kwargs):
        self.records.append(("debug", message % args if args else message))

    def info(self, message, *args, **kwargs):
        self.records.append(("info", message % args if args else message))

    def warning(self, message, *args, **kwargs):
        self.records.append(("warning", message % args if args else message))


class _Ctx:
    def __init__(self) -> None:
        self.logger = _Logger()


def _make_plugin():
    """构造已注入默认配置的插件，捕获 _api_post 出站 payload。"""
    plugin = QQOfficialAdapterPlugin()
    plugin.set_plugin_config(plugin.get_default_config())
    plugin._ready = True
    plugin._id_map = {"group": {}, "user": {}}
    plugin._ref_idx = {}
    plugin._replies = {}
    plugin._reply_lock = asyncio.Lock()
    plugin._ctx = _Ctx()
    captured = []

    async def _fake_api_post(path, payload):
        captured.append((path, dict(payload)))
        return {"id": "ROBOT1.0_out", "ext_info": {"ref_idx": "REFIDX_out=="}}

    plugin._api_post = _fake_api_post
    return plugin, captured


def _group_message(text: str = "你好"):
    return {
        "message_info": {"group_info": {"group_id": GROUP_OPENID}, "additional_config": {}},
        "raw_message": [{"type": "text", "data": text}],
    }


@pytest.mark.asyncio
async def test_group_without_passive_window_sends_proactive() -> None:
    # 没有任何入站记录（例如定时任务主动推送）：不能再裸发无参请求，
    # 必须显式带 msg_seq，否则平台按默认值 1 去重直接失败。
    plugin, captured = _make_plugin()
    result = await plugin.handle_qq_official_gateway(_group_message())
    assert result["success"] is True
    _, payload = captured[0]
    assert payload["msg_type"] == 0
    assert "msg_id" not in payload
    low, high = PROACTIVE_MSG_SEQ_RANGE
    assert low <= payload["msg_seq"] <= high


@pytest.mark.asyncio
async def test_group_with_expired_passive_window_sends_proactive() -> None:
    # 入站记录存在但已超出群聊 5 分钟被动窗口，同样退化为主动消息。
    plugin, captured = _make_plugin()
    plugin._replies[("group", GROUP_OPENID, REPLY_ID)] = (time.time() - 3600, 0)
    result = await plugin.handle_qq_official_gateway(_group_message())
    assert result["success"] is True
    _, payload = captured[0]
    assert "msg_id" not in payload
    assert "msg_seq" in payload


@pytest.mark.asyncio
async def test_group_with_exhausted_passive_quota_sends_proactive() -> None:
    # 被动回复序号已用满（群聊每条入站最多 5 次），第 6 条必须走主动消息。
    plugin, captured = _make_plugin()
    plugin._replies[("group", GROUP_OPENID, REPLY_ID)] = (time.time(), 5)
    result = await plugin.handle_qq_official_gateway(_group_message())
    assert result["success"] is True
    _, payload = captured[0]
    assert "msg_id" not in payload
    assert "msg_seq" in payload


@pytest.mark.asyncio
async def test_private_without_passive_window_sends_proactive() -> None:
    # 单聊走 /v2/users 路径，主动推送同样只带 msg_seq。
    plugin, captured = _make_plugin()
    message = {
        "message_info": {"additional_config": {"platform_io_target_user_id": USER_OPENID}},
        "raw_message": [{"type": "text", "data": "在吗"}],
    }
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is True
    path, payload = captured[0]
    assert path == f"/v2/users/{USER_OPENID}/messages"
    assert "msg_id" not in payload
    assert "msg_seq" in payload


@pytest.mark.asyncio
async def test_passive_window_keeps_passive_fields() -> None:
    # 被动窗口可用时必须优先走被动回复，不得改用主动额度。
    plugin, captured = _make_plugin()
    plugin._replies[("group", GROUP_OPENID, REPLY_ID)] = (time.time(), 0)
    message = {
        "message_info": {"group_info": {"group_id": GROUP_OPENID}, "additional_config": {}},
        "raw_message": [{"type": "reply", "data": REPLY_ID}, {"type": "text", "data": "你好"}],
    }
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is True
    _, payload = captured[0]
    assert payload["msg_id"] == REPLY_ID
    assert payload["msg_seq"] == 1
    assert all("主动消息" not in text for _, text in plugin._ctx.logger.records)


@pytest.mark.asyncio
async def test_proactive_disabled_fails_with_readable_error() -> None:
    # 关闭开关后主动发送直接失败，并把原因带回宿主，而不是静默丢消息。
    plugin, captured = _make_plugin()
    plugin.config.proactive.enabled = False
    result = await plugin.handle_qq_official_gateway(_group_message())
    assert result["success"] is False
    assert "主动消息已关闭" in result["error"]
    assert captured == []


@pytest.mark.asyncio
async def test_proactive_msg_seq_varies_between_sends() -> None:
    # 连续主动推送必须换序号，否则平台按 msg_id+msg_seq 去重会失败。
    plugin, captured = _make_plugin()
    for _ in range(8):
        await plugin.handle_qq_official_gateway(_group_message())
    sequences = {payload["msg_seq"] for _, payload in captured}
    assert len(sequences) > 1
