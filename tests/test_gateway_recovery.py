from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncio
import time

import pytest

from plugins.qq_official_adapter.plugin import QQOfficialAdapterPlugin


def make_plugin() -> QQOfficialAdapterPlugin:
    plugin = QQOfficialAdapterPlugin()
    plugin.set_plugin_config(plugin.get_default_config())
    plugin._set_context(SimpleNamespace(
        logger=Mock(),
        gateway=SimpleNamespace(update_state=AsyncMock(return_value=True), route_message=AsyncMock(return_value=True)),
    ))
    plugin._account_id, plugin._bot_name = "bot", "Bot"
    plugin._session_id, plugin._seq = "old-session", 10
    plugin._access_token, plugin._token_expires_at = "old-token", time.time() + 3600
    return plugin


@asynccontextmanager
async def context(value):
    yield value


def test_inflight_message_survives_disconnect_and_waits_for_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        plugin = make_plugin()
        await plugin._set_ready(True)
        started, release = asyncio.Event(), asyncio.Event()

        async def attachments(data):
            started.set()
            await release.wait()
            return [], [], False, False

        monkeypatch.setattr(plugin, "_build_attachments", attachments)
        await plugin._handle_gateway_payload({
            "op": 0, "s": 11, "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {"id": "msg", "group_openid": "group", "author": {"member_openid": "user"},
                  "content": "hello", "timestamp": "2026-10-28T00:00:00Z"},
        })
        task, = plugin._dispatch_tasks
        await started.wait()
        await plugin._disconnect()
        assert plugin._seq == 11
        assert not task.done()
        plugin._handle_gateway_close(4009)
        # 后续状态变化不能改变已接收消息的解码身份。
        plugin._account_id, plugin._bot_name = "", ""
        release.set()
        await asyncio.sleep(0)
        assert not task.done()
        plugin.ctx.gateway.route_message.assert_not_awaited()
        await plugin._handle_gateway_payload({
            "op": 0, "s": 1, "t": "READY",
            "d": {"session_id": "new-session", "user": {"id": "bot", "username": "Bot"}},
        })
        await asyncio.wait_for(task, 1)
        plugin.ctx.gateway.route_message.assert_awaited_once()
        sent = plugin.ctx.gateway.route_message.call_args.kwargs
        assert sent["message"]["account_id"] == "bot"
        assert sent["dedupe_key"] == "msg"
        await plugin._stop()
    asyncio.run(run())


def test_stop_cancels_inflight_route_before_offline_lock() -> None:
    async def run() -> None:
        plugin = make_plugin()
        await plugin._set_ready(True)
        started = asyncio.Event()

        async def route(**kwargs):
            started.set()
            await asyncio.Event().wait()

        plugin.ctx.gateway.route_message.side_effect = route
        await plugin._handle_gateway_payload({
            "op": 0, "s": 11, "t": "C2C_MESSAGE_CREATE",
            "d": {"id": "msg", "author": {"user_openid": "user"}, "content": "hello", "timestamp": "2026-10-28T00:00:00Z"},
        })
        task, = plugin._dispatch_tasks
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(plugin._stop(), 1)
        assert task.cancelled()
        assert not plugin._dispatch_tasks
        assert not plugin._inbound_ready.is_set()
    asyncio.run(run())


@pytest.mark.parametrize("code, clear_session, clear_token, retry", [
    (1000, False, False, True), (1006, False, False, True),
    (4004, False, True, True), (4006, True, True, True),
    (4007, True, True, True), (4009, True, True, True),
    (4900, True, True, True), (4913, True, True, True),
    (4008, False, False, True), (4914, False, False, False), (4915, False, False, False),
])
def test_close_codes(code: int, clear_session: bool, clear_token: bool, retry: bool) -> None:
    plugin = make_plugin()
    assert plugin._handle_gateway_close(code) is retry
    assert plugin._session_id == ("" if clear_session else "old-session")
    assert plugin._seq == (None if clear_session else 10)
    assert plugin._access_token == ("" if clear_token else "old-token")
    assert plugin._account_id == "bot"
    if clear_token:
        assert plugin._token_expires_at == 0


@pytest.mark.parametrize("resumable", [True, False])
def test_invalid_session_honors_resumable_flag(resumable: bool) -> None:
    async def run() -> None:
        plugin = make_plugin()
        plugin._ws = SimpleNamespace(close=AsyncMock())
        await plugin._handle_gateway_payload({"op": 9, "d": resumable})
        assert plugin._session_id == ("old-session" if resumable else "")
        assert plugin._access_token == ("old-token" if resumable else "")
        plugin._ws.close.assert_awaited_once()
    asyncio.run(run())


def test_connect_returns_close_code_when_iterator_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    class Socket:
        close_code = 4009

        async def __aiter__(self):
            for frame in []:
                yield frame

    async def run() -> None:
        plugin = make_plugin()
        response = SimpleNamespace(raise_for_status=Mock(), json=AsyncMock(return_value={"url": "wss://gateway.example"}))
        plugin._session = SimpleNamespace(closed=False, get=lambda *a, **k: context(response), ws_connect=lambda *a, **k: context(Socket()))
        monkeypatch.setattr(plugin, "_authorize", AsyncMock(return_value="token"))
        assert await plugin._connect() == 4009
    asyncio.run(run())


@pytest.mark.parametrize("code, delay", [(4004, 5.0), (4008, 60.0)])
def test_retry_loop_refreshes_token_delays_and_stops_on_fatal(monkeypatch: pytest.MonkeyPatch, code: int, delay: float) -> None:
    async def run() -> None:
        plugin = make_plugin()
        session = SimpleNamespace()
        monkeypatch.setattr("plugins.qq_official_adapter.plugin.ClientSession", lambda **k: context(session))
        sleep = AsyncMock()
        monkeypatch.setattr("plugins.qq_official_adapter.plugin.asyncio.sleep", sleep)
        calls = 0

        async def connect():
            nonlocal calls
            calls += 1
            if calls == 1:
                return code
            assert plugin._access_token == ("" if code == 4004 else "old-token")
            return 4915

        monkeypatch.setattr(plugin, "_connect", connect)
        await plugin._run()
        assert calls == 2
        sleep.assert_awaited_once_with(delay)
        assert plugin._session is None
    asyncio.run(run())


@pytest.mark.parametrize("code, opcode", [(4004, 6), (4007, 2), (4009, 2)])
def test_reconnect_selects_resume_or_identify(monkeypatch: pytest.MonkeyPatch, code: int, opcode: int) -> None:
    async def run() -> None:
        plugin = make_plugin()
        plugin._handle_gateway_close(code)
        plugin._access_token = "refreshed-token"
        send = AsyncMock()
        monkeypatch.setattr(plugin, "_send_ws", send)
        await plugin._handle_gateway_payload({"op": 10, "d": {"heartbeat_interval": 30000}})
        packet = send.call_args.args[0]
        assert packet["op"] == opcode
        assert packet["d"]["token"] == "QQBot refreshed-token"
        await plugin._stop()
    asyncio.run(run())
