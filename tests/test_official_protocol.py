from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.qq_official_adapter.plugin import QQOfficialAdapterPlugin


@pytest.mark.asyncio
async def test_ready_reports_official_protocol():
    plugin = QQOfficialAdapterPlugin()
    plugin.set_plugin_config(plugin.get_default_config())
    gateway = SimpleNamespace(update_state=AsyncMock(return_value=True))
    plugin._set_context(SimpleNamespace(logger=Mock(), gateway=gateway))
    plugin._account_id = "bot"
    await plugin._set_ready(True)
    assert gateway.update_state.call_args.kwargs["metadata"]["protocol"] == "qq_official"
