import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.qq_official_adapter.codec import is_bot_author
from plugins.qq_official_adapter.plugin import QQOfficialAdapterPlugin


def _make_plugin(*, ignore_other_bots: bool):
    plugin = QQOfficialAdapterPlugin()
    config = plugin.get_default_config()
    config["filter"]["ignore_other_bots"] = ignore_other_bots
    plugin.set_plugin_config(config)
    gateway = SimpleNamespace(update_state=AsyncMock(return_value=True), route_message=AsyncMock(return_value=True))
    plugin._set_context(SimpleNamespace(logger=Mock(), gateway=gateway))
    plugin._account_id = "bot"
    plugin._bot_name = "麦麦"
    plugin._ready = True
    plugin._inbound_ready.set()
    return plugin, gateway


def _payload(*, bot: bool = False) -> dict:
    return {
        "id": "msg-1",
        "author": {"member_openid": "member-1", "username": "小明", "bot": bot},
        "content": "你好",
        "group_openid": "group-1",
        "timestamp": "2026-11-04T00:00:00+08:00",
    }


@pytest.mark.parametrize(
    ("author", "expected"),
    [
        ({"bot": True}, True),
        ({"bot": False}, False),
        ({"bot": "true"}, True),
        ({"bot": "false"}, False),
        ({"username": "no-bot-field"}, False),
        ({"bot": None}, False),
    ],
)
def test_is_bot_author(author: dict, expected: bool) -> None:
    assert is_bot_author({"author": author}) is expected


def test_is_bot_author_handles_missing_author() -> None:
    assert is_bot_author({}) is False
    assert is_bot_author({"author": "not-a-mapping"}) is False


@pytest.mark.asyncio
async def test_other_bot_message_is_dropped_when_enabled() -> None:
    plugin, gateway = _make_plugin(ignore_other_bots=True)

    await plugin._receive_message("GROUP_AT_MESSAGE_CREATE", _payload(bot=True), account_id="bot", bot_name="麦麦")

    assert gateway.route_message.await_count == 0


@pytest.mark.asyncio
async def test_other_bot_message_passes_when_disabled() -> None:
    plugin, gateway = _make_plugin(ignore_other_bots=False)

    await plugin._receive_message("GROUP_AT_MESSAGE_CREATE", _payload(bot=True), account_id="bot", bot_name="麦麦")

    assert gateway.route_message.await_count == 1


@pytest.mark.asyncio
async def test_human_message_is_not_filtered() -> None:
    plugin, gateway = _make_plugin(ignore_other_bots=True)

    await plugin._receive_message("GROUP_AT_MESSAGE_CREATE", _payload(bot=False), account_id="bot", bot_name="麦麦")

    assert gateway.route_message.await_count == 1


@pytest.mark.asyncio
async def test_other_bot_command_is_not_executed() -> None:
    plugin, gateway = _make_plugin(ignore_other_bots=True)
    plugin._handle_assign_command = AsyncMock()
    payload = _payload(bot=True)
    payload["content"] = "/assign_id 10001"

    await plugin._receive_message("GROUP_AT_MESSAGE_CREATE", payload, account_id="bot", bot_name="麦麦")

    assert plugin._handle_assign_command.await_count == 0
    assert gateway.route_message.await_count == 0
