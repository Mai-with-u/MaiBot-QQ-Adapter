from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Tuple
from unittest.mock import Mock

import time

import pytest

from plugins.qq_official_adapter.plugin import QQOfficialAdapterPlugin

GROUP_OPENID = "group-openid"
REPLY_ID = "ROBOT1.0_reply"
REFIDX = "REFIDX_reply=="


async def _make_plugin() -> Tuple[QQOfficialAdapterPlugin, List[Tuple[str, Dict[str, Any]]]]:
    """沿用上游正文/Markdown回归，使用真实初始化及作用域缓存。"""
    plugin = QQOfficialAdapterPlugin()
    plugin.set_plugin_config(plugin.get_default_config())
    plugin._ready = True
    plugin._account_id = "test-bot"
    plugin._id_map = {"group": {}, "user": {}}
    plugin._set_context(SimpleNamespace(logger=Mock()))
    # 模拟刚收到 REPLY_ID（被动窗口凭证 + 可用 REFIDX）。
    plugin._replies[("group", GROUP_OPENID, REPLY_ID)] = (time.time(), 0)
    plugin._references.remember(("test-bot", "group", GROUP_OPENID), REPLY_ID, REFIDX)
    captured: List[Tuple[str, Dict[str, Any]]] = []

    async def _fake_api_post(path: str, payload: Mapping[str, Any], **kwargs: Any) -> Dict[str, Any]:
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
            {"type": "reply", "data": {"target_message_id": REPLY_ID}},
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
            {"type": "reply", "data": {"target_message_id": REPLY_ID}},
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
