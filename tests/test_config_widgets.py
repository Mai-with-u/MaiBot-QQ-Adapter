from unittest.mock import AsyncMock

import asyncio
import pytest

from maibot_sdk.config_widgets import ConfigWidgetSnapshot

from plugins.qq_official_adapter.plugin import QQOfficialAdapterPlugin
from plugins.qq_official_adapter.qr_login import render_qr


def test_config_widgets_render_png_and_keep_secrets_private():
    async def run():
        plugin = QQOfficialAdapterPlugin()
        config = plugin.get_default_config()
        config["qq_official"]["app_secret"] = "private-test-secret"
        plugin.set_plugin_config(config)
        plugin._qr_login.state = "waiting"
        plugin._qr_login.image = await asyncio.to_thread(render_qr, "https://example.com/login")
        states = await plugin.get_config_widget_state()
        snapshot = ConfigWidgetSnapshot(widgets=plugin.config_widgets, states=states)
        assert states["login"].image.startswith("data:image/png;base64,")
        assert states["login_cancel"].visible
        assert states["reconnect"].disabled
        assert "private-test-secret" not in snapshot.model_dump_json()
        assert "runtime_widgets" not in plugin.get_default_config()
        plugin._ready = True
        plugin._qr_login.state = "success"
        plugin._qr_login.image = ""
        assert not (await plugin.get_config_widget_state())["login"].visible

    asyncio.run(run())


def test_config_actions_use_existing_login_flow():
    async def run():
        plugin = QQOfficialAdapterPlugin()
        plugin.webui_login_start = AsyncMock()
        plugin.webui_login_cancel = AsyncMock()
        await plugin.on_config_widget_action("login_start")
        await plugin.on_config_widget_action("login_cancel")
        plugin.webui_login_start.assert_awaited_once()
        plugin.webui_login_cancel.assert_awaited_once()
        with pytest.raises(ValueError):
            await plugin.on_config_widget_action("invalid")

    asyncio.run(run())
