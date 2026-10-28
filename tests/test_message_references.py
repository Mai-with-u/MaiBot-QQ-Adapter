from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List, Mapping, Tuple
from unittest.mock import AsyncMock, Mock

import asyncio
import base64

import pytest

from plugins.qq_official_adapter.codec import decode_message, extract_command_text, summarize_payload
from plugins.qq_official_adapter.plugin import QQOfficialAdapterPlugin
from plugins.qq_official_adapter.references import ReferenceCache

GROUP_EVENT = "GROUP_MESSAGE_CREATE"
SCOPE = ("bot", "group", "raw-group")


def make_plugin() -> QQOfficialAdapterPlugin:
    plugin = QQOfficialAdapterPlugin()
    plugin.set_plugin_config(plugin.get_default_config())
    plugin._set_context(SimpleNamespace(
        logger=Mock(),
        gateway=SimpleNamespace(update_state=AsyncMock(return_value=True), route_message=AsyncMock(return_value=True)),
    ))
    plugin._account_id = "bot"
    plugin._bot_name = "Bot"
    return plugin


def inbound(message_id: str = "current", index: str = "current-index",
            reference: str = "old-index") -> Dict[str, Any]:
    return {
        "id": message_id, "group_openid": "raw-group", "author": {"member_openid": "raw-user"},
        "content": "current body", "timestamp": "2026-10-28T00:00:00Z",
        "message_scene": {"ext": [f"msg_idx={index}", f"ref_msg_idx={reference}"]},
    }


def outbound(segments: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"message_info": {"group_info": {"group_id": "mapped-group"}}, "raw_message": segments,
            "reply_to": "passive-trigger"}


REPLY = {"type": "reply", "data": {"target_message_id": "old-message"}}
TEXT = {"type": "text", "data": "new body"}
IMAGE = {"type": "image", "binary_data_base64": base64.b64encode(b"GIF89a-test-image").decode()}
AT = {"type": "at", "data": {"target_user_id": "raw-user"}}


@pytest.mark.asyncio
async def test_inbound_standard_reply_uses_raw_scope_not_unified_ids() -> None:
    plugin = make_plugin()
    await plugin._set_ready(True)
    plugin._id_map = {"group": {"raw-group": "mapped-group"}, "user": {"raw-user": "mapped-user"}}
    plugin._unified_account_id = "shared-bot"
    plugin._references.remember(SCOPE, "old-message", "old-index")
    data = inbound()
    data["msg_elements"] = [{"message_type": 103, "content": "/old-command", "attachments": [
        {"content_type": "image/png", "url": "https://old.invalid/signed?secret=hidden"}]}]
    plugin._download_attachment = AsyncMock()
    await plugin._receive_message(GROUP_EVENT, data, account_id="bot", bot_name="Bot")
    message = plugin.ctx.gateway.route_message.call_args.kwargs["message"]
    assert message["account_id"] == "shared-bot"
    assert message["raw_message"] == [REPLY, {"type": "text", "data": "current body"}]
    assert message["processed_plain_text"] == "current body"
    assert not message["is_picture"] and not message["is_command"]
    plugin._download_attachment.assert_not_awaited()
    assert plugin._references.id_for(SCOPE, "current-index") == "current"
    assert plugin._references.id_for(("shared-bot", "group", "mapped-group"), "current-index") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["TMP_old-index", "missing", "current-index"])
async def test_miss_or_self_reference_keeps_current_body(reference: str) -> None:
    plugin = make_plugin()
    await plugin._set_ready(True)
    plugin._references.remember(SCOPE, "old-message", "old-index")
    await plugin._receive_message(GROUP_EVENT, inbound(reference=reference), account_id="bot", bot_name="Bot")
    message = plugin.ctx.gateway.route_message.call_args.kwargs["message"]
    assert message["raw_message"] == [{"type": "text", "data": "current body"}]


@pytest.mark.asyncio
async def test_image_observed_before_download_and_gateway_acceptance() -> None:
    plugin = make_plugin()
    await plugin._set_ready(True)
    started, release = asyncio.Event(), asyncio.Event()

    async def attachments(data: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], List[str], bool, bool]:
        if data["id"] == "old-message":
            started.set()
            await release.wait()
            return [{"type": "image", "data": "", "hash": "test"}], ["[图片]"], True, False
        return [], [], False, False

    plugin._build_attachments = attachments
    await plugin._handle_gateway_payload({"op": 0, "t": GROUP_EVENT,
        "d": inbound("old-message", "old-index", "unknown")})
    old_task, = plugin._dispatch_tasks
    # Observation happens even before the task begins, not after the image await.
    assert plugin._references.id_for(SCOPE, "old-index") == "old-message"
    await started.wait()
    await plugin._receive_message(GROUP_EVENT, inbound(), account_id="bot", bot_name="Bot")
    routed = plugin.ctx.gateway.route_message.call_args.kwargs["message"]
    assert routed["raw_message"][0] == REPLY
    # Referenced image is not copied into the newer message; host may not have stored the old image yet.
    assert not routed["is_picture"]
    release.set()
    await old_task
    plugin.ctx.gateway.route_message.return_value = False
    await plugin._receive_message(GROUP_EVENT, inbound("rejected", "rejected-index"), account_id="bot", bot_name="Bot")
    assert plugin._references.id_for(SCOPE, "rejected-index") == "rejected"


@pytest.mark.asyncio
async def test_conflict_during_image_download_rechecked_before_route() -> None:
    plugin = make_plugin()
    await plugin._set_ready(True)
    plugin._references.remember(SCOPE, "old-message", "old-index")

    async def attachments(data: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], List[str], bool, bool]:
        plugin._references.remember(SCOPE, "different-message", "old-index")
        return [], [], False, False

    plugin._build_attachments = attachments
    await plugin._receive_message(GROUP_EVENT, inbound(), account_id="bot", bot_name="Bot")
    assert plugin.ctx.gateway.route_message.call_args.kwargs["message"]["raw_message"] == [
        {"type": "text", "data": "current body"}]


@pytest.mark.asyncio
async def test_current_image_mentions_and_command_preserved() -> None:
    plugin = make_plugin()
    await plugin._set_ready(True)
    plugin._references.remember(SCOPE, "old-message", "old-index")
    plugin._download_attachment = AsyncMock(return_value=b"current-image")
    data = inbound()
    data["content"] = "<@bot> /hello <@friend>"
    data["mentions"] = [{"id": "friend", "username": "Friend"}]
    data["attachments"] = [{"content_type": "image/png", "url": "https://grouppro.grouppro.qq.com/current"}]
    await plugin._receive_message(GROUP_EVENT, data, account_id="bot", bot_name="Bot")
    message = plugin.ctx.gateway.route_message.call_args.kwargs["message"]
    assert message["is_at"] and message["is_picture"] and message["is_command"]
    assert message["raw_message"][0] == REPLY
    assert [part["type"] for part in message["raw_message"]] == ["reply", "at", "text", "at", "image"]
    assert base64.b64decode(message["raw_message"][-1]["binary_data_base64"]) == b"current-image"
    plugin._handle_assign_command = AsyncMock()
    data["content"] = "/assign_group_id test-group"
    await plugin._receive_message(GROUP_EVENT, data, account_id="bot", bot_name="Bot")
    plugin._handle_assign_command.assert_awaited_once()
    assert plugin.ctx.gateway.route_message.await_count == 1


def test_quoted_old_text_and_mentions_are_not_current_content() -> None:
    data = inbound()
    data["content"] = ""
    data["msg_elements"] = [
        {"message_type": 103, "text": "/assign_id old", "mentions": {"id": "bot"},
         "msg_elements": [{"type": "at", "id": "bot"}]},
        {"text_element": {"content": "new body"}},
    ]
    assert extract_command_text(data) == "new body"
    message, _, _ = decode_message(GROUP_EVENT, data, "bot")
    assert message["raw_message"] == [TEXT]
    assert not message["is_at"] and not message["is_command"]


@pytest.mark.asyncio
@pytest.mark.parametrize("segments, expected", [
    ([REPLY, TEXT], [True]), ([REPLY, IMAGE], [True]), ([REPLY, AT, TEXT], [False]),
    ([REPLY, TEXT, IMAGE, TEXT], [True, False, False]), ([REPLY, AT, IMAGE], [False, False]),
    ([TEXT], [False]),
])
async def test_outbound_reference_body_types_split_and_response_cache(
    segments: List[Dict[str, Any]], expected: List[bool]
) -> None:
    plugin = make_plugin()
    plugin._ready = True
    plugin._id_map = {"group": {"raw-group": "mapped-group"}, "user": {}}
    plugin._references.remember(SCOPE, "old-message", "old-index")
    plugin._claim_passive_fields = AsyncMock(return_value={"msg_id": "passive-trigger", "msg_seq": 3})
    plugin._upload_image = AsyncMock(return_value="uploaded-file")
    counter = 0

    async def post(path: str, payload: Mapping[str, Any], **kwargs: Any) -> Dict[str, Any]:
        nonlocal counter
        counter += 1
        return {"id": f"sent-{counter}", "ext_info": {"ref_idx": f"sent-index-{counter}=="}}

    plugin._api_post = AsyncMock(side_effect=post)
    result = await plugin.handle_qq_official_gateway(outbound(segments))
    assert result == {"success": True, "external_message_id": "sent-1"}
    calls = plugin._api_post.call_args_list
    assert len(calls) == len(expected)
    for number, (call, has_reference) in enumerate(zip(calls, expected), start=1):
        path, body = call.args
        assert path == "/v2/groups/raw-group/messages"
        assert ("message_reference" in body) is has_reference
        if has_reference:
            assert body["message_reference"] == {"message_id": "old-index"}
        assert body["msg_id"] == "passive-trigger" and body["msg_seq"] == 3
        assert plugin._references.id_for(SCOPE, f"sent-index-{number}==") == f"sent-{number}"
        assert plugin._references.index_for(SCOPE, f"sent-{number}") == f"sent-index-{number}=="
    plugin._claim_passive_fields.assert_awaited_with("group", "mapped-group", "passive-trigger")


@pytest.mark.asyncio
async def test_bot_receipt_can_be_quoted_exactly_but_not_by_prefix_equivalence() -> None:
    plugin = make_plugin()
    await plugin._set_ready(True)
    plugin._api_post = AsyncMock(return_value={"id": "bot-response", "ext_info": {"ref_idx": "REFIDX_receipt=="}})
    result = await plugin.handle_qq_official_gateway({
        "message_info": {"group_info": {"group_id": "raw-group"}}, "raw_message": [TEXT]})
    assert result["success"]
    await plugin._receive_message(GROUP_EVENT, inbound(reference="REFIDX_receipt=="), account_id="bot", bot_name="Bot")
    assert plugin.ctx.gateway.route_message.call_args.kwargs["message"]["raw_message"][0] == {
        "type": "reply", "data": {"target_message_id": "bot-response"}}
    await plugin._receive_message(GROUP_EVENT, inbound("other", "other-index", "TMP_receipt=="), account_id="bot", bot_name="Bot")
    assert plugin.ctx.gateway.route_message.call_args.kwargs["message"]["raw_message"][0]["type"] == "text"


@pytest.mark.asyncio
async def test_passive_reply_to_is_not_a_visual_reference_and_private_scope() -> None:
    plugin = make_plugin()
    plugin._ready = True
    scope = ("bot", "private", "raw-user")
    plugin._references.remember(scope, "passive-trigger", "passive-index")
    plugin._api_post = AsyncMock(return_value={"id": "sent", "ext_info": {"ref_idx": "sent-index"}})
    message = {"message_info": {"additional_config": {"platform_io_target_user_id": "raw-user"}},
               "raw_message": [TEXT], "reply_to": "passive-trigger"}
    assert (await plugin.handle_qq_official_gateway(message))["success"]
    assert "message_reference" not in plugin._api_post.call_args.args[1]
    assert plugin._references.id_for(scope, "sent-index") == "sent"
    assert plugin._references.id_for(("bot", "group", "raw-user"), "sent-index") == ""
    await plugin._set_ready(True, force=True)
    data = inbound(reference="sent-index")
    data["author"] = {"user_openid": "raw-user"}
    await plugin._receive_message("C2C_MESSAGE_CREATE", data, account_id="bot", bot_name="Bot")
    assert plugin.ctx.gateway.route_message.call_args.kwargs["message"]["raw_message"][0] == {
        "type": "reply", "data": {"target_message_id": "sent"}}


@pytest.mark.asyncio
async def test_lifecycle_reconnect_keeps_cache_account_switch_and_stop_clear() -> None:
    plugin = make_plugin()
    plugin._references.remember(SCOPE, "old-message", "old-index")
    await plugin._disconnect()
    plugin._reset_gateway_session()
    assert plugin._references.id_for(SCOPE, "old-index") == "old-message"
    await plugin._handle_gateway_payload({"op": 0, "t": "READY", "d": {
        "session_id": "session", "user": {"id": "bot"}}})
    assert plugin._references.id_for(SCOPE, "old-index") == "old-message"
    await plugin._handle_gateway_payload({"op": 0, "t": "READY", "d": {
        "session_id": "other-session", "user": {"id": "other-bot"}}})
    assert plugin._references.id_for(SCOPE, "old-index") == ""
    assert plugin.ctx.gateway.update_state.call_args.kwargs["account_id"] == "other-bot"
    plugin._references.remember(("other-bot", "group", "raw-group"), "a", "i")
    await plugin._stop()
    assert not plugin._references._forward and not plugin._references._reverse


@pytest.mark.asyncio
async def test_account_switch_during_upload_does_not_send_or_store_old_receipt() -> None:
    plugin = make_plugin()
    plugin._ready = True
    plugin._id_map["group"]["raw-group"] = "mapped-group"

    async def upload(*args: Any) -> str:
        await plugin._handle_gateway_payload({"op": 0, "t": "READY", "d": {
            "session_id": "new", "user": {"id": "other-bot"}}})
        return "file"

    plugin._upload_image = upload
    plugin._api_post = AsyncMock()
    result = await plugin.handle_qq_official_gateway(outbound([REPLY, IMAGE]))
    assert not result["success"]
    plugin._api_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_account_switch_during_download_discards_old_dispatch() -> None:
    plugin = make_plugin()
    await plugin._set_ready(True)

    async def attachments(data: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], List[str], bool, bool]:
        await plugin._handle_gateway_payload({"op": 0, "t": "READY", "d": {
            "session_id": "new", "user": {"id": "other-bot"}}})
        return [], [], False, False

    plugin._build_attachments = attachments
    await plugin._receive_message(GROUP_EVENT, inbound(), account_id="bot", bot_name="Bot")
    plugin.ctx.gateway.route_message.assert_not_awaited()
    assert not plugin._replies
    assert not plugin._references._forward


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["conflict", "ttl", "capacity", "unchanged"])
async def test_reference_rechecked_after_authorization(change: str) -> None:
    plugin = make_plugin()
    plugin._ready = True
    plugin._id_map["group"]["raw-group"] = "mapped-group"
    now = [0.0]
    plugin._references = ReferenceCache(capacity=1, ttl=10, clock=lambda: now[0])
    plugin._references.remember(SCOPE, "old-message", "old-index")
    plugin._claim_passive_fields = AsyncMock(return_value={"msg_id": "passive-trigger", "msg_seq": 1})
    started, release = asyncio.Event(), asyncio.Event()
    bodies: List[Dict[str, Any]] = []

    async def authorize() -> str:
        started.set()
        await release.wait()
        return "test-token"

    @asynccontextmanager
    async def post(url: str, *, headers: Mapping[str, str], json: Dict[str, Any]) -> AsyncIterator[SimpleNamespace]:
        bodies.append(json)
        yield SimpleNamespace(status=200, text=AsyncMock(return_value='{"id":"sent"}'))

    plugin._authorize = authorize
    plugin._session = SimpleNamespace(closed=False, post=post)
    task = asyncio.create_task(plugin.handle_qq_official_gateway(outbound([REPLY, TEXT])))
    await asyncio.wait_for(started.wait(), 1)
    if change == "conflict":
        plugin._references.remember(SCOPE, "different-message", "old-index")
    elif change == "ttl":
        now[0] = 10
    elif change == "capacity":
        plugin._references.remember(SCOPE, "different-message", "different-index")
    release.set()
    result = await asyncio.wait_for(task, 1)
    assert result == {"success": True, "external_message_id": "sent"}
    expected: Dict[str, Any] = {
        "msg_type": 0, "content": "new body", "msg_id": "passive-trigger", "msg_seq": 1,
    }
    if change == "unchanged":
        expected["message_reference"] = {"message_id": "old-index"}
    assert bodies == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["miss", "expired", "conflict", "other-scope"])
async def test_unavailable_outbound_reference_keeps_text(reason: str) -> None:
    plugin = make_plugin()
    plugin._ready = True
    plugin._id_map["group"]["raw-group"] = "mapped-group"
    now = [0.0]
    plugin._references = ReferenceCache(ttl=10, clock=lambda: now[0])
    if reason != "miss":
        scope = SCOPE if reason != "other-scope" else ("other-bot", "group", "raw-group")
        plugin._references.remember(scope, "old-message", "old-index")
    if reason == "expired":
        now[0] = 10
    if reason == "conflict":
        plugin._references.remember(SCOPE, "old-message", "conflicting-index")
    plugin._api_post = AsyncMock(return_value={"id": "sent"})
    assert (await plugin.handle_qq_official_gateway(outbound([REPLY, TEXT])))["success"]
    body = plugin._api_post.call_args.args[1]
    assert body["content"] == "new body" and "message_reference" not in body


@pytest.mark.asyncio
async def test_send_receipt_after_account_switch_is_not_cached() -> None:
    plugin = make_plugin()
    plugin._ready = True

    async def post(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        await plugin._handle_gateway_payload({"op": 0, "t": "READY", "d": {
            "session_id": "new", "user": {"id": "other-bot"}}})
        return {"id": "sent", "ext_info": {"ref_idx": "sent-index"}}

    plugin._api_post = post
    assert (await plugin.handle_qq_official_gateway(outbound([TEXT])))["success"]
    assert not plugin._references._forward


@pytest.mark.asyncio
async def test_failure_logs_and_return_preserve_error_details() -> None:
    plugin = make_plugin()
    plugin._ready = True
    error = RuntimeError("test send failure detail")
    plugin._api_post = AsyncMock(side_effect=error)
    result = await plugin.handle_qq_official_gateway(outbound([REPLY, TEXT]))
    assert not result["success"]
    assert result["error"] == str(error)
    assert plugin.ctx.logger.warning.call_args.args[1] is error
    assert plugin.ctx.logger.warning.call_args.kwargs["exc_info"] is True
    secret = "test-private-event-value"
    assert secret not in summarize_payload({"id": secret, "attachments": [{"url": secret}], secret: secret})


@pytest.mark.asyncio
async def test_api_error_preserves_http_status_code_and_message() -> None:
    plugin = make_plugin()

    @asynccontextmanager
    async def response(*args: Any, **kwargs: Any) -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(status=400, text=AsyncMock(return_value='{"code":40001,"message":"test API failure"}'))

    plugin._session = SimpleNamespace(closed=False, post=response)
    plugin._authorize = AsyncMock(return_value="token")
    with pytest.raises(RuntimeError) as error:
        await plugin._api_post("/v2/groups/private-openid/messages", {})
    assert "HTTP 400" in str(error.value)
    assert "code=40001" in str(error.value)
    assert "message=test API failure" in str(error.value)


@pytest.mark.asyncio
async def test_partial_send_retains_count_error_and_traceback() -> None:
    plugin = make_plugin()
    plugin._ready = True
    plugin._upload_image = AsyncMock(return_value="test-file")
    error = RuntimeError("second fragment failed")
    plugin._api_post = AsyncMock(side_effect=[{"id": "sent"}, error])
    result = await plugin.handle_qq_official_gateway(outbound([TEXT, IMAGE]))
    assert not result["success"]
    assert result["error"] == "已发送 1 条后失败: second fragment failed"
    assert plugin.ctx.logger.warning.call_args.args[1] is error
    assert plugin.ctx.logger.warning.call_args.kwargs["exc_info"] is True


@pytest.mark.asyncio
async def test_dispatch_failure_preserves_original_traceback() -> None:
    plugin = make_plugin()

    async def fail() -> None:
        raise ValueError("test route failure")

    task = asyncio.create_task(fail())
    with pytest.raises(ValueError, match="test route failure"):
        await task
    plugin._on_dispatch_done(task)
    error_type, error, traceback = plugin.ctx.logger.error.call_args.kwargs["exc_info"]
    assert error_type is ValueError
    assert error is task.exception()
    assert traceback is error.__traceback__ and traceback is not None


@pytest.mark.asyncio
async def test_api_non_json_error_retains_original_bounded_detail() -> None:
    plugin = make_plugin()
    body = "test upstream failure " + "x" * 250

    @asynccontextmanager
    async def response(*args: Any, **kwargs: Any) -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(status=502, text=AsyncMock(return_value=body))

    plugin._session = SimpleNamespace(closed=False, post=response)
    plugin._authorize = AsyncMock(return_value="test-token")
    with pytest.raises(RuntimeError) as error:
        await plugin._api_post("/v2/groups/test-group/messages", {})
    assert str(error.value) == f"QQ 接口 messages 失败: HTTP 502 {body[:200]}"


@pytest.mark.asyncio
async def test_api_success_without_account_change() -> None:
    plugin = make_plugin()
    bodies: List[Dict[str, Any]] = []

    @asynccontextmanager
    async def response(url: str, *, headers: Mapping[str, str], json: Dict[str, Any]) -> AsyncIterator[SimpleNamespace]:
        assert url.endswith("/messages")
        assert headers["Authorization"] == "QQBot test-token"
        bodies.append(json)
        yield SimpleNamespace(status=200, text=AsyncMock(return_value='{"id":"sent"}'))

    plugin._session = SimpleNamespace(closed=False, post=response)
    plugin._authorize = AsyncMock(return_value="test-token")
    assert await plugin._api_post("/v2/groups/test-group/messages", {"msg_type": 0, "content": "hello"}) == {"id": "sent"}
    assert bodies == [{"msg_type": 0, "content": "hello"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["account", "restart"])
async def test_account_change_during_authorization_does_not_post(change: str) -> None:
    plugin = make_plugin()
    plugin._ready = True
    plugin._id_map["group"]["raw-group"] = "mapped-group"
    plugin._references.remember(SCOPE, "old-message", "old-index")
    started, release = asyncio.Event(), asyncio.Event()

    async def authorize() -> str:
        started.set()
        await release.wait()
        return "test-token"

    plugin._authorize = authorize
    post = Mock()
    plugin._session = SimpleNamespace(closed=False, post=post)
    task = asyncio.create_task(plugin.handle_qq_official_gateway(outbound([REPLY, TEXT])))
    await asyncio.wait_for(started.wait(), 1)
    if change == "account":
        await plugin._handle_gateway_payload({"op": 0, "t": "READY", "d": {
            "session_id": "new", "user": {"id": "other-bot"}}})
    else:
        await plugin._stop()
        plugin._account_id = "bot"
        await plugin._set_ready(True)
    release.set()
    result = await asyncio.wait_for(task, 1)
    post.assert_not_called()
    assert not result["success"]
    assert "账号" in result["error"]
    assert not plugin._references._forward
