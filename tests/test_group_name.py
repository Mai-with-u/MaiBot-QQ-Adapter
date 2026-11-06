import asyncio
import time
from types import SimpleNamespace

import pytest

from plugins.qq_official_adapter.codec import decode_message
from plugins.qq_official_adapter.plugin import GROUP_NAME_TTL_SEC, QQOfficialAdapterPlugin


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class _Ctx:
    logger = _Logger()


class _RouteRecorder:
    """记录网关投递的消息，替代 SDK 的 MessageGateway。"""

    def __init__(self) -> None:
        self.messages = []

    async def route_message(self, *, gateway_name, message, external_message_id, dedupe_key):
        self.messages.append(message)
        return True


async def _make_plugin(monkeypatch: pytest.MonkeyPatch, handler):
    """构造只启用群名链路的插件，用 handler 替代 _api_get。"""
    plugin = QQOfficialAdapterPlugin.__new__(QQOfficialAdapterPlugin)
    plugin._account_id, plugin._bot_name = "bot", "Bot"
    plugin._unified_account_id = ""
    plugin._id_map = {"group": {}, "user": {}}
    plugin._replies = {}
    plugin._ref_idx = {}
    plugin._reply_lock = asyncio.Lock()
    plugin._group_names = {}
    plugin._group_name_lock = asyncio.Lock()
    plugin._ready = True
    plugin._inbound_ready = asyncio.Event()
    plugin._inbound_ready.set()
    plugin._gateway_state_lock = asyncio.Lock()
    plugin._ctx = SimpleNamespace(logger=_Logger(), gateway=_RouteRecorder())
    plugin._api_get = handler
    return plugin


def _group_payload(**overrides) -> dict:
    payload = {
        "id": "msg",
        "group_openid": "group-openid",
        "author": {"member_openid": "user-openid", "nickname": "小明"},
        "content": "你好",
        "timestamp": "2026-11-06T00:00:00Z",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_group_message_fills_group_name_from_info_api(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def handler(path: str):
        calls.append(path)
        return {"group_openid": "group-openid", "group_name": "读书分享会"}

    plugin = await _make_plugin(monkeypatch, handler)
    await plugin._receive_message("GROUP_AT_MESSAGE_CREATE", _group_payload(), account_id="bot", bot_name="Bot")

    assert calls == ["/v2/groups/group-openid/info"]
    routed = plugin.ctx.gateway.messages[0]
    assert routed["message_info"]["group_info"]["group_name"] == "读书分享会"


@pytest.mark.asyncio
async def test_group_name_is_cached_across_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def handler(path: str):
        calls.append(path)
        return {"group_name": "读书分享会"}

    plugin = await _make_plugin(monkeypatch, handler)
    for index in range(3):
        await plugin._receive_message(
            "GROUP_MESSAGE_CREATE", _group_payload(id=f"msg-{index}"), account_id="bot", bot_name="Bot"
        )

    assert calls == ["/v2/groups/group-openid/info"]
    assert len(plugin.ctx.gateway.messages) == 3


@pytest.mark.asyncio
async def test_group_name_failure_keeps_message_deliverable(monkeypatch: pytest.MonkeyPatch) -> None:
    """该接口仅对白名单机器人开放（无权限为 11253），失败时必须照常投递消息。"""
    calls = []

    async def handler(path: str):
        calls.append(path)
        raise RuntimeError('QQ 接口 info 失败: HTTP 403 code=11253 message=no permission')

    plugin = await _make_plugin(monkeypatch, handler)
    await plugin._receive_message("GROUP_AT_MESSAGE_CREATE", _group_payload(), account_id="bot", bot_name="Bot")

    routed = plugin.ctx.gateway.messages[0]
    assert routed["message_info"]["group_info"]["group_name"] == ""
    # 未开通权限时也不能每条消息都重试，用短 TTL 的负缓存兜住。
    await plugin._receive_message(
        "GROUP_AT_MESSAGE_CREATE", _group_payload(id="msg-2"), account_id="bot", bot_name="Bot"
    )
    assert calls == ["/v2/groups/group-openid/info"]
    assert len(plugin.ctx.gateway.messages) == 2


@pytest.mark.asyncio
async def test_empty_group_name_is_retried_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """接口返回空群名时按 TTL 缓存，过期后重查。"""
    calls = []

    async def handler(path: str):
        calls.append(path)
        return {"group_name": "读书分享会"}

    plugin = await _make_plugin(monkeypatch, handler)
    plugin._group_names["group-openid"] = ("", time.time())
    assert await plugin._get_group_name("group-openid") == ""
    assert calls == []

    plugin._group_names["group-openid"] = ("", time.time() - GROUP_NAME_TTL_SEC - 1)
    assert await plugin._get_group_name("group-openid") == "读书分享会"
    assert calls == ["/v2/groups/group-openid/info"]


@pytest.mark.asyncio
async def test_private_message_does_not_query_group_info(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def handler(path: str):
        calls.append(path)
        return {}

    plugin = await _make_plugin(monkeypatch, handler)
    await plugin._receive_message(
        "C2C_MESSAGE_CREATE",
        {
            "id": "msg",
            "author": {"user_openid": "user-openid", "nickname": "小明"},
            "content": "你好",
            "timestamp": "2026-11-06T00:00:00Z",
        },
        account_id="bot",
        bot_name="Bot",
    )

    assert calls == []
    assert "group_info" not in plugin.ctx.gateway.messages[0]["message_info"]


def test_decode_message_prefers_injected_group_name() -> None:
    payload = _group_payload()
    payload["group_name"] = "事件里的群名"
    message, _, _ = decode_message("GROUP_AT_MESSAGE_CREATE", payload, "bot", group_name="接口查到的群名")
    assert message["message_info"]["group_info"]["group_name"] == "接口查到的群名"

    # 未注入时退回事件字段，两者都没有则为空串（主程序保留聊天流已记录的群名）。
    message, _, _ = decode_message("GROUP_AT_MESSAGE_CREATE", payload, "bot")
    assert message["message_info"]["group_info"]["group_name"] == "事件里的群名"

    payload.pop("group_name")
    message, _, _ = decode_message("GROUP_AT_MESSAGE_CREATE", payload, "bot")
    assert message["message_info"]["group_info"]["group_name"] == ""

    message, _, _ = decode_message("C2C_MESSAGE_CREATE", {
        "id": "msg",
        "author": {"user_openid": "user-openid"},
        "content": "你好",
        "timestamp": "2026-11-06T00:00:00Z",
    }, "bot", group_name="不该出现")
    assert "group_info" not in message["message_info"]
