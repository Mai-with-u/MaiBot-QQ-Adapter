import time
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from qq_official_adapter.codec import encode_message, validate_ark
from qq_official_adapter.plugin import QQOfficialAdapterPlugin


def card():
    return {"template_id": 37, "kv": [
        {"key": "#METATITLE#", "value": "视频标题"},
        {"key": "#METACOVER#", "value": "https://i0.hdslb.com/cover.jpg"},
        {"key": "#METAURL#", "value": "https://www.bilibili.com/video/BV1S9xT6zE4e"}]}


def message(payload, group=False):
    return {"message_info": {"group_info": {"group_id": "mapped-group"} if group else None,
        "additional_config": {"platform_io_target_user_id": "mapped-user"}},
        "raw_message": [{"type": "dict", "data": {"type": "qq_ark", "data": payload}}]}


@pytest.mark.parametrize("group", [False, True])
def test_sdk_dict_component_becomes_ark_without_changing_target(group):
    value = card()
    scene, target, parts, _ = encode_message(message(value, group),
        reverse_map={"group": {"mapped-group": "group-openid"}, "user": {"mapped-user": "user-openid"}})
    assert scene == ("group" if group else "private")
    assert target == ("group-openid" if group else "user-openid")
    assert parts == [{"kind": "body", "body": {"msg_type": 3, "ark": value}}]
    value["kv"][0]["value"] = "changed later"
    assert parts[0]["body"]["ark"]["kv"][0]["value"] == "视频标题"


def test_text_card_text_preserves_order_and_direct_segments():
    msg = message(card())
    msg["raw_message"] = [{"type": "text", "data": "before"}, {"type": "qq_ark", "data": card()},
        {"type": "text", "data": "after"}]
    assert [part["body"]["msg_type"] for part in encode_message(msg)[2]] == [0, 3, 0]


def test_official_array_template_is_supported_without_recursive_objects():
    value = {"template_id": 23, "kv": [{"key": "#LIST#", "obj": [{"obj_kv": [
        {"key": "desc", "value": "item"}, {"key": "link", "value": "https://example.com"}]}]}]}
    assert validate_ark(value) == value


@pytest.mark.parametrize("payload", [None, "{}", {"template_id": True, "kv": []},
    {"template_id": 0, "kv": []}, {"template_id": 37, "kv": []},
    {"template_id": 37, "kv": [{"key": "x", "value": 1}]},
    {"template_id": 37, "kv": [{"key": "x", "value": "a" * 4097}]},
    {"template_id": 37, "kv": [{"key": "x", "value": "v", "obj": []}]},
    {"template_id": 37, "kv": [{"key": "x", "obj": [{"obj_kv": [{"key": "x", "obj": []}]}]}]},
    {**card(), "msg_id": "override"}, {**card(), "target": "another-user"},
    {"template_id": 37, "kv": [{"key": "x", "value": "a" * 4096}] * 50}])
def test_invalid_or_routing_payloads_are_rejected(payload):
    with pytest.raises(ValueError):
        encode_message(message(payload))


@pytest.mark.asyncio
async def test_gateway_uses_existing_passive_reply_accounting():
    plugin = QQOfficialAdapterPlugin.__new__(QQOfficialAdapterPlugin)
    plugin._ready = True
    plugin._id_map = {"user": {}, "group": {}}
    plugin._ref_idx = {}
    plugin._replies = {("private", "mapped-user", "incoming"): (time.time(), 0)}
    plugin._reply_lock = asyncio.Lock()
    plugin._ctx = SimpleNamespace(logger=Mock())
    plugin._api_post = AsyncMock(return_value={"id": "qq-card-id"})
    result = await plugin.handle_qq_official_gateway(message(card()))
    assert result == {"success": True, "external_message_id": "qq-card-id"}
    path, body = plugin._api_post.call_args.args
    assert path == "/v2/users/mapped-user/messages"
    assert body["msg_type"] == 3 and body["ark"] == card()
    assert body["msg_id"] == "incoming" and body["msg_seq"] == 1


@pytest.mark.asyncio
async def test_ark_does_not_quote_implicit_anchor():
    plugin = QQOfficialAdapterPlugin.__new__(QQOfficialAdapterPlugin)
    plugin._ready = True
    plugin._id_map = {"user": {}, "group": {}}
    plugin._ref_idx = {}
    plugin._replies = {("private", "mapped-user", "incoming"): (time.time(), 0)}
    plugin._reply_lock = asyncio.Lock()
    plugin._ctx = SimpleNamespace(logger=Mock())
    plugin._store_ref_idx("incoming", "REFIDX_incoming==")
    plugin._api_post = AsyncMock(return_value={"id": "qq-card-id"})
    msg = message(card())
    msg["reply_to"] = "incoming"
    assert (await plugin.handle_qq_official_gateway(msg))["success"] is True
    payload = plugin._api_post.call_args.args[1]
    assert payload["msg_type"] == 3
    assert "message_reference" not in payload
    assert payload["msg_id"] == "incoming" and payload["msg_seq"] == 1
