import asyncio
import time

import pytest

from qq_official_adapter.plugin import QQOfficialAdapterPlugin

GROUP_OPENID = "group-openid"
REPLY_ID = "ROBOT1.0_reply"
REFIDX = "REFIDX_reply=="


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class _Ctx:
    logger = _Logger()


async def _make_plugin():
    """构造绕过 SDK 初始化的插件，捕获 _api_post 出站 payload。"""
    plugin = QQOfficialAdapterPlugin.__new__(QQOfficialAdapterPlugin)
    plugin._ready = True
    plugin._id_map = {"group": {}, "user": {}}
    plugin._ref_idx = {}
    plugin._replies = {}
    plugin._reply_lock = asyncio.Lock()
    plugin._ctx = _Ctx()
    # 模拟刚收到 REPLY_ID（被动窗口凭证 + 可用 REFIDX）。
    plugin._replies[("group", GROUP_OPENID, REPLY_ID)] = (time.time(), 0)
    plugin._store_ref_idx(REPLY_ID, REFIDX)
    captured = []

    async def _fake_api_post(path, payload):
        captured.append((path, dict(payload)))
        return {"id": "ROBOT1.0_out", "ext_info": {"ref_idx": "REFIDX_out=="}}

    plugin._api_post = _fake_api_post
    return plugin, captured


@pytest.mark.asyncio
async def test_gateway_text_keeps_message_reference() -> None:
    # 纯文本（msg_type=0）的引用气泡正常，必须继续携带 message_reference。
    plugin, captured = await _make_plugin()
    message = {
        "message_info": {"group_info": {"group_id": GROUP_OPENID}, "additional_config": {}},
        "raw_message": [
            {"type": "reply", "data": REPLY_ID},
            {"type": "text", "data": "你好"},
        ],
    }
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is True
    _, payload = captured[0]
    assert payload["msg_type"] == 0
    assert payload["message_reference"] == {"message_id": REFIDX}


@pytest.mark.asyncio
async def test_gateway_markdown_mention_omits_message_reference() -> None:
    # 含 @ 的消息会被 codec 转成 markdown（msg_type=2），携带引用会在 Mac QQ /
    # 部分手机 QQ 重复渲染正文，故不得携带 message_reference，但保留被动凭证。
    plugin, captured = await _make_plugin()
    message = {
        "message_info": {"group_info": {"group_id": GROUP_OPENID}, "additional_config": {}},
        "raw_message": [
            {"type": "reply", "data": REPLY_ID},
            {"type": "at", "data": {"target_user_id": "user-openid"}},
            {"type": "text", "data": "看一下"},
        ],
    }
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is True
    _, payload = captured[0]
    assert payload["msg_type"] == 2
    assert "qqbot-at-user" in payload["markdown"]["content"]
    assert "message_reference" not in payload
    assert payload.get("msg_id") == REPLY_ID
