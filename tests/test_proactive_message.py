import asyncio
import base64
import time

import pytest

from qq_official_adapter.plugin import (
    PASSIVE_EXPIRED_CODES,
    PROACTIVE_MSG_SEQ_RANGE,
    QQAPIError,
    QQOfficialAdapterPlugin,
)

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


def _make_plugin(reject_code: int = 0, reject_times: int = 0):
    """构造已注入默认配置的插件，捕获 _api_post 出站 payload。

    前 reject_times 次发送按 reject_code 这个业务错误码失败，用于验证被动转主动的重发。
    """
    plugin = QQOfficialAdapterPlugin()
    plugin.set_plugin_config(plugin.get_default_config())
    plugin._ready = True
    plugin._id_map = {"group": {}, "user": {}}
    plugin._ref_idx = {}
    plugin._replies = {}
    plugin._reply_lock = asyncio.Lock()
    plugin._ctx = _Ctx()
    captured = []
    rejected = [0]

    async def _fake_api_post(path, payload):
        captured.append((path, dict(payload)))
        if rejected[0] < reject_times:
            rejected[0] += 1
            raise QQAPIError(
                f"QQ 接口 messages 失败: HTTP 400 code={reject_code}", code=reject_code, status=400
            )
        return {"id": "ROBOT1.0_out", "ext_info": {"ref_idx": "REFIDX_out=="}}

    plugin._api_post = _fake_api_post
    return plugin, captured


def _group_message(text: str = "你好"):
    return {
        "message_info": {"group_info": {"group_id": GROUP_OPENID}, "additional_config": {}},
        "raw_message": [{"type": "text", "data": text}],
    }


def _group_reply_message(text: str = "你好"):
    """带 reply 段的群聊消息：宿主明确指定了引用目标，被动回复可用。"""
    return {
        "message_info": {"group_info": {"group_id": GROUP_OPENID}, "additional_config": {}},
        "raw_message": [{"type": "reply", "data": REPLY_ID}, {"type": "text", "data": text}],
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


@pytest.mark.asyncio
@pytest.mark.parametrize("code", sorted(PASSIVE_EXPIRED_CODES))
async def test_passive_rejected_by_platform_retries_as_proactive(code: int) -> None:
    # 本地窗口仍认为被动可用，平台却判定 msg_id 过期或次数超限：改按主动消息重发一次，
    # 否则这些消息会直接失败，定时任务和真回复都会被一起吞掉。
    plugin, captured = _make_plugin(reject_code=code, reject_times=1)
    plugin._replies[("group", GROUP_OPENID, REPLY_ID)] = (time.time(), 0)
    result = await plugin.handle_qq_official_gateway(_group_reply_message())
    assert result["success"] is True
    assert len(captured) == 2
    _, passive_payload = captured[0]
    assert passive_payload["msg_id"] == REPLY_ID
    _, proactive_payload = captured[1]
    assert "msg_id" not in proactive_payload
    assert proactive_payload["msg_seq"] in range(
        PROACTIVE_MSG_SEQ_RANGE[0], PROACTIVE_MSG_SEQ_RANGE[1] + 1
    )
    assert any("改按主动消息重发" in text for _, text in plugin._ctx.logger.records)


@pytest.mark.asyncio
async def test_non_passive_error_is_not_retried() -> None:
    # 内容违规、主动额度用尽之类的错误重发也不会有不同结果，必须原样上报给宿主。
    plugin, captured = _make_plugin(reject_code=40034105, reject_times=99)
    plugin._replies[("group", GROUP_OPENID, REPLY_ID)] = (time.time(), 0)
    result = await plugin.handle_qq_official_gateway(_group_reply_message())
    assert result["success"] is False
    assert "40034105" in result["error"]
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_proactive_disabled_after_passive_rejection_surfaces_error() -> None:
    # 被动被平台拒绝但用户关了主动消息开关：不能静默丢消息，要把原因带回宿主。
    plugin, captured = _make_plugin(reject_code=40034024, reject_times=1)
    plugin.config.proactive.enabled = False
    plugin._replies[("group", GROUP_OPENID, REPLY_ID)] = (time.time(), 0)
    result = await plugin.handle_qq_official_gateway(_group_reply_message())
    assert result["success"] is False
    assert "主动消息已关闭" in result["error"]
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_retry_does_not_repeat_already_sent_parts() -> None:
    # 多分片消息里只重发失败的那一片：已经发出去的文本不能再发一遍。
    plugin, captured = _make_plugin(reject_code=40034005, reject_times=1)
    plugin._replies[("group", GROUP_OPENID, REPLY_ID)] = (time.time(), 0)

    async def _fake_upload(base_path, binary, file_name):
        return "FILEINFO_image"

    plugin._upload_image = _fake_upload
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8).decode()
    message = {
        "message_info": {"group_info": {"group_id": GROUP_OPENID}, "additional_config": {}},
        "raw_message": [
            {"type": "reply", "data": REPLY_ID},
            {"type": "text", "data": "第一段"},
            {"type": "image", "data": {"file": "a.png"}, "binary_data_base64": png},
        ],
    }
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is True
    assert [payload["msg_type"] for _, payload in captured] == [0, 0, 7]
    assert captured[0][1]["msg_id"] == REPLY_ID
    assert "msg_id" not in captured[1][1]
    assert captured[2][1]["msg_id"] == REPLY_ID
