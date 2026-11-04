import asyncio
import json
import time

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from plugins.qq_official_adapter.codec import (
    GROUP_JOIN_REQUEST,
    GROUP_MEMBER_ADD,
    GROUP_MEMBER_REMOVE,
    build_group_notice_message,
    decode_group_event,
)
from plugins.qq_official_adapter.plugin import GROUP_EVENT_INTENTS, MAX_GROUP_EVENTS, MESSAGE_INTENTS, QQOfficialAdapterPlugin

GROUP_OPENID = "group-openid"
UNIFIED_GROUP = "100200300"
MEMBER_OPENID = "member-openid"


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class _FakeResponse:
    def __init__(self, payload: Any = None, status: int = 200) -> None:
        self.status = status
        self._payload = payload if payload is not None else {}
        self._text = json.dumps(self._payload) if payload is not None else ""

    async def text(self) -> str:
        return self._text


class _FakeRequest:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False


class _FakeSession:
    """记录所有出站请求，并按顺序回放响应。"""

    closed = False

    def __init__(self, payload: Any = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._payload = payload if payload is not None else {}

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeRequest:
        self.calls.append({"method": method, "url": url, **kwargs})
        return _FakeRequest(_FakeResponse(self._payload))

    def post(self, url: str, **kwargs: Any) -> _FakeRequest:
        return self.request("POST", url, **kwargs)


def _make_plugin(*, ready: bool = True) -> QQOfficialAdapterPlugin:
    plugin = QQOfficialAdapterPlugin()
    plugin.set_plugin_config(plugin.get_default_config())
    plugin._set_context(
        SimpleNamespace(
            logger=_Logger(),
            gateway=SimpleNamespace(
                route_message=_MockAsync(True),
                update_state=_MockAsync(True),
            ),
            message=SimpleNamespace(get_by_id=_MockAsync({})),
        )
    )
    plugin._account_id = "bot"
    plugin._bot_name = "Bot"
    plugin._access_token = "token"
    plugin._token_expires_at = time.time() + 3600
    session = _FakeSession()
    plugin._session = session  # type: ignore[assignment]
    plugin._ready = ready
    if ready:
        plugin._inbound_ready.set()
    return plugin


class _MockAsync:
    """极简异步替身：记录调用参数并返回固定值。"""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: List[Dict[str, Any]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append({"args": args, "kwargs": kwargs})
        return self.result


def _member_add_payload() -> Dict[str, Any]:
    return {
        "timestamp": 1762250000,
        "group_openid": GROUP_OPENID,
        "member_openid": MEMBER_OPENID,
    }


def test_decode_group_event_member_add_maps_unified_ids():
    record = decode_group_event(
        GROUP_MEMBER_ADD,
        _member_add_payload(),
        event_id="EVENT1",
        id_map={"group": {GROUP_OPENID: UNIFIED_GROUP}, "user": {MEMBER_OPENID: "10001"}},
    )

    assert record["event"] == GROUP_MEMBER_ADD
    assert record["label"] == "群成员加入"
    assert record["event_id"] == "EVENT1"
    assert record["group_openid"] == GROUP_OPENID
    assert record["group_id"] == UNIFIED_GROUP
    assert record["member_id"] == "10001"
    assert record["timestamp"] == 1762250000.0
    assert record["summary"] == "[事件-群成员加入] member-openid 加入了群聊"


def test_decode_group_event_join_request_keeps_review_fields():
    record = decode_group_event(
        GROUP_JOIN_REQUEST,
        {
            "group_openid": GROUP_OPENID,
            "join_request_id": "REQ1",
            "member_openid": MEMBER_OPENID,
            "username": "小明",
            "apply_at": "2026-11-05T10:00:00+08:00",
            "apply_source": "self_apply",
            "risk_tips": "注意核实身份",
            "bot": False,
            "verify_info": {"method": "verify_message"},
        },
    )

    assert record["join_request_id"] == "REQ1"
    assert record["apply_source"] == "self_apply"
    assert record["verify_info"] == {"method": "verify_message"}
    assert record["summary"] == "[事件-加群申请] 小明 申请加群（安全提示：注意核实身份）"
    assert record["timestamp"] > 0


def test_decode_group_event_rejects_unknown_event():
    with pytest.raises(ValueError):
        decode_group_event("GROUP_AT_MESSAGE_CREATE", _member_add_payload())


def test_build_group_notice_message_marks_notify_and_targets_group():
    record = decode_group_event(GROUP_MEMBER_ADD, _member_add_payload(), event_id="EVENT1")
    message = build_group_notice_message(record, "bot", unified_account_id="10000")

    assert message["is_notify"] is True
    assert message["account_id"] == "10000"
    assert message["message_id"] == "EVENT1"
    assert message["message_info"]["user_info"]["user_nickname"] == MEMBER_OPENID
    assert message["message_info"]["group_info"]["group_id"] == GROUP_OPENID
    additional_config = message["message_info"]["additional_config"]
    assert additional_config["qq_official_notice_type"] == GROUP_MEMBER_ADD
    assert additional_config["qq_official_notice_payload"]["member_openid"] == MEMBER_OPENID
    assert message["raw_message"][0]["type"] == "text"


def test_handle_group_event_records_and_routes_notice():
    plugin = _make_plugin()

    async def run() -> None:
        await plugin._handle_group_event(GROUP_MEMBER_ADD, _member_add_payload(), "EVENT1")
        assert plugin._group_event_serial == 1
        assert plugin._group_events[0]["serial"] == 1
        await asyncio.gather(*tuple(plugin._dispatch_tasks))

    asyncio.run(run())

    routed = plugin.ctx.gateway.route_message.calls[0]["kwargs"]
    assert routed["message"]["is_notify"] is True
    assert routed["external_message_id"] == "EVENT1"
    assert routed["dedupe_key"] == "EVENT1"


def test_handle_group_event_respects_notify_switch():
    plugin = _make_plugin()
    nested = plugin.get_default_config()
    nested["group_admin"]["notify_events"] = False
    plugin.set_plugin_config(nested)

    async def run() -> None:
        await plugin._handle_group_event(GROUP_MEMBER_REMOVE, _member_add_payload(), "EVENT2")
        await asyncio.gather(*tuple(plugin._dispatch_tasks))

    asyncio.run(run())

    assert plugin.ctx.gateway.route_message.calls == []
    assert plugin._group_events[-1]["event"] == GROUP_MEMBER_REMOVE


def test_snapshot_group_events_is_incremental_and_bounded():
    plugin = _make_plugin()

    async def run() -> None:
        for index in range(5):
            await plugin._handle_group_event(GROUP_MEMBER_ADD, _member_add_payload(), f"EVENT{index}")

    asyncio.run(run())

    snapshot = plugin.snapshot_group_events(after_serial=2, limit=2)
    assert [item["serial"] for item in snapshot] == [3, 4]
    assert [item["serial"] for item in plugin.snapshot_group_events(after_serial=4)] == [5]
    assert plugin.snapshot_group_events(after_serial=5) == []
    assert len(plugin.snapshot_group_events(limit=999)) == 5


def test_group_event_buffer_drops_oldest_entries():
    plugin = _make_plugin()

    async def run() -> None:
        for index in range(MAX_GROUP_EVENTS + 3):
            await plugin._handle_group_event(GROUP_MEMBER_ADD, _member_add_payload(), f"EVENT{index}")

    asyncio.run(run())

    assert len(plugin._group_events) == MAX_GROUP_EVENTS
    assert plugin._group_events[0]["serial"] == 4


def test_gateway_payload_records_group_event():
    plugin = _make_plugin()

    async def run() -> None:
        await plugin._handle_gateway_payload({"op": 0, "t": GROUP_MEMBER_ADD, "d": _member_add_payload(), "id": "EVENT9"})
        await asyncio.gather(*tuple(plugin._dispatch_tasks))

    asyncio.run(run())

    assert plugin._group_events[0]["event_id"] == "EVENT9"


def test_identify_subscribes_group_events():
    plugin = _make_plugin()
    sent: List[Dict[str, Any]] = []

    async def fake_send(payload: Dict[str, Any]) -> None:
        sent.append(payload)

    plugin._send_ws = fake_send

    async def run() -> None:
        await plugin._handle_gateway_payload({"op": 10, "d": {"heartbeat_interval": 30000}})

    asyncio.run(run())
    for task in tuple(plugin._dispatch_tasks):
        task.cancel()
    if plugin._heartbeat_task is not None:
        plugin._heartbeat_task.cancel()

    assert sent[0]["op"] == 2
    assert sent[0]["d"]["intents"] == GROUP_EVENT_INTENTS == MESSAGE_INTENTS | (1 << 24)


def test_group_event_intent_downgrades_on_rejection():
    plugin = _make_plugin()

    assert plugin._resolve_intents() == GROUP_EVENT_INTENTS
    assert plugin._handle_gateway_close(4014) is True
    assert plugin._group_events_denied is True
    assert plugin._group_events_active is False
    assert plugin._resolve_intents() == MESSAGE_INTENTS


def test_group_events_disabled_skips_subscription():
    plugin = _make_plugin()
    nested = plugin.get_default_config()
    nested["group_admin"]["events_enabled"] = False
    plugin.set_plugin_config(nested)

    async def run() -> None:
        await plugin._handle_gateway_payload({"op": 0, "t": GROUP_MEMBER_ADD, "d": _member_add_payload(), "id": "EVENT3"})
        await plugin._handle_gateway_payload({"op": 0, "t": GROUP_JOIN_REQUEST, "d": _member_add_payload(), "id": "EVENT4"})

    asyncio.run(run())

    assert plugin._resolve_intents() == MESSAGE_INTENTS
    assert plugin._group_events == []


def test_api_group_info_resolves_unified_group_id():
    plugin = _make_plugin()
    plugin._id_map = {"group": {GROUP_OPENID: UNIFIED_GROUP}, "user": {}}

    async def run() -> Dict[str, Any]:
        return await plugin.api_group_info(group_openid=UNIFIED_GROUP)

    result = asyncio.run(run())

    assert result["success"] is True
    assert plugin._session.calls[0]["method"] == "GET"
    assert plugin._session.calls[0]["url"].endswith(f"/v2/groups/{GROUP_OPENID}/info")


def test_api_group_mute_clear_sends_del_with_empty_expire():
    plugin = _make_plugin()

    async def run() -> Dict[str, Any]:
        return await plugin.api_group_mute_clear(group_openid=GROUP_OPENID, member_openids=[MEMBER_OPENID])

    result = asyncio.run(run())

    assert result["success"] is True
    body = plugin._session.calls[0]["json"]
    assert body == {"members": [{"op": "del", "member_openid": MEMBER_OPENID, "mute_expire_at": ""}]}
    assert plugin._session.calls[0]["url"].endswith("/restrict_chat_setting")


def test_api_group_mute_set_requires_expire_at_for_add():
    plugin = _make_plugin()
    nested = plugin.get_default_config()
    nested["mute"]["allowed_groups"] = []

    async def run() -> None:
        with pytest.raises(ValueError):
            await plugin.api_group_mute_set(
                group_openid=GROUP_OPENID, members=[{"op": "add", "member_openid": MEMBER_OPENID}]
            )

    asyncio.run(run())
    assert plugin._session.calls == []


def test_api_group_members_remove_splits_requests():
    plugin = _make_plugin()
    members = [f"openid-{index}" for index in range(21)]

    async def run() -> Dict[str, Any]:
        return await plugin.api_group_members_remove(group_openid=GROUP_OPENID, member_openids=members)

    result = asyncio.run(run())

    assert result["success"] is True
    assert result["data"]["removed_members"] == 21
    assert len(plugin._session.calls) == 2
    assert len(plugin._session.calls[0]["json"]["member_openids"]) == 20


def test_tool_unmute_sends_del_operation():
    plugin = _make_plugin()
    plugin.ctx.message.get_by_id = _MockAsync(
        {
            "message_info": {
                "user_info": {"user_id": "10001", "user_nickname": "小明"},
                "group_info": {"group_id": UNIFIED_GROUP},
                "additional_config": {
                    "qq_official_group_openid": GROUP_OPENID,
                    "qq_official_user_openid": MEMBER_OPENID,
                },
            }
        }
    )

    async def run() -> Dict[str, Any]:
        return await plugin.tool_unmute(msg_id="MSG1", stream_id="stream-1")

    result = asyncio.run(run())

    assert result["success"] is True
    assert "已解除" in result["content"]
    body = plugin._session.calls[0]["json"]
    assert body["members"][0]["op"] == "del"
    assert body["members"][0]["mute_expire_at"] == ""


def test_tool_unmute_rejects_non_group_message():
    plugin = _make_plugin()
    plugin.ctx.message.get_by_id = _MockAsync(
        {"message_info": {"user_info": {"user_id": "10001", "user_nickname": "小明"}}}
    )

    async def run() -> Dict[str, Any]:
        return await plugin.tool_unmute(msg_id="MSG1")

    result = asyncio.run(run())

    assert result["success"] is False
    assert "仅支持群聊" in result["error"]
    assert plugin._session.calls == []
