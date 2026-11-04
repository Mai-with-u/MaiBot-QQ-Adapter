from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.qq_official_adapter.plugin import QQOfficialAdapterPlugin


def make_plugin(app_id: str = "102000000") -> QQOfficialAdapterPlugin:
    plugin = QQOfficialAdapterPlugin()
    plugin.set_plugin_config(plugin.get_default_config())
    plugin.config.qq_official.app_id = app_id
    plugin._set_context(SimpleNamespace(logger=Mock(), gateway=SimpleNamespace(update_state=AsyncMock(return_value=True))))
    return plugin


@pytest.mark.asyncio
async def test_numeric_user_uses_qlogo_number_route():
    plugin = make_plugin()
    resource = await plugin.api_get_avatar("qq", "10001")
    assert resource["status"] == "available"
    assert resource["url"] == "https://q1.qlogo.cn/g?b=qq&nk=10001&s=640"
    assert resource["expires_in"] == 86400


@pytest.mark.asyncio
async def test_numeric_group_uses_group_route():
    plugin = make_plugin()
    resource = await plugin.api_get_avatar("qq", "20002", "group")
    assert resource["status"] == "available"
    assert resource["url"] == "https://p.qlogo.cn/gh/20002/20002/640"


@pytest.mark.asyncio
async def test_bound_openid_falls_back_to_numeric_route():
    plugin = make_plugin()
    plugin._id_map["user"]["OPENID_1"] = "30003"
    resource = await plugin.api_get_avatar("qq", "OPENID_1")
    assert resource["url"] == "https://q1.qlogo.cn/g?b=qq&nk=30003&s=640"


@pytest.mark.asyncio
async def test_unbound_openid_user_uses_qqapp_route():
    plugin = make_plugin()
    resource = await plugin.api_get_avatar("qq", "OPENID_2")
    assert resource["status"] == "available"
    assert resource["url"] == "https://q.qlogo.cn/qqapp/102000000/OPENID_2/100"


@pytest.mark.asyncio
async def test_openid_requires_configured_app_id():
    plugin = make_plugin(app_id="")
    assert await plugin.api_get_avatar("qq", "OPENID_2") == {"status": "unsupported"}


@pytest.mark.asyncio
async def test_openid_group_is_unsupported():
    plugin = make_plugin()
    assert await plugin.api_get_avatar("qq", "GROUP_OPENID", "group") == {"status": "unsupported"}


@pytest.mark.asyncio
async def test_bot_target_uses_users_me_avatar(monkeypatch: pytest.MonkeyPatch):
    plugin = make_plugin()
    plugin._account_id = "BOT_OPENID"
    avatar = "https://thirdqq.qlogo.cn/g?b=oidb&k=abc&kti=def&s=0&t=1"
    get = AsyncMock(return_value={"id": "BOT_OPENID", "username": "Bot", "avatar": avatar})
    monkeypatch.setattr(plugin, "_api_get", get)

    resource = await plugin.api_get_avatar("qq", "BOT_OPENID")

    assert get.await_args.args == ("/users/@me",)
    assert resource == {"status": "available", "url": avatar, "expires_in": 86400}


@pytest.mark.asyncio
async def test_unified_account_id_is_treated_as_bot(monkeypatch: pytest.MonkeyPatch):
    plugin = make_plugin()
    plugin._account_id = "BOT_OPENID"
    plugin._unified_account_id = "10086"
    plugin._id_map["user"]["BOT_OPENID"] = "10086"
    avatar = "https://thirdqq.qlogo.cn/g?b=oidb&k=abc&kti=def&s=0&t=1"
    monkeypatch.setattr(plugin, "_api_get", AsyncMock(return_value={"avatar": avatar}))

    resource = await plugin.api_get_avatar("qq", "10086")

    assert resource["url"] == avatar


@pytest.mark.asyncio
async def test_bot_avatar_is_cached_between_calls(monkeypatch: pytest.MonkeyPatch):
    plugin = make_plugin()
    plugin._account_id = "BOT_OPENID"
    get = AsyncMock(return_value={"avatar": "https://thirdqq.qlogo.cn/g?b=oidb&k=abc&kti=def&s=0&t=1"})
    monkeypatch.setattr(plugin, "_api_get", get)

    await plugin.api_get_avatar("qq", "BOT_OPENID")
    await plugin.api_get_avatar("qq", "BOT_OPENID")

    assert get.await_count == 1


@pytest.mark.asyncio
async def test_bot_avatar_missing_when_profile_query_fails(monkeypatch: pytest.MonkeyPatch):
    plugin = make_plugin()
    plugin._account_id = "BOT_OPENID"
    monkeypatch.setattr(plugin, "_api_get", AsyncMock(side_effect=RuntimeError("HTTP 500")))

    assert await plugin.api_get_avatar("qq", "BOT_OPENID") == {"status": "missing"}


@pytest.mark.asyncio
async def test_other_platform_is_unsupported():
    plugin = make_plugin()
    assert await plugin.api_get_avatar("telegram", "10001") == {"status": "unsupported"}


@pytest.mark.asyncio
async def test_invalid_target_type_raises():
    plugin = make_plugin()
    with pytest.raises(ValueError):
        await plugin.api_get_avatar("qq", "10001", "channel")


@pytest.mark.asyncio
async def test_non_positive_numeric_target_raises():
    plugin = make_plugin()
    with pytest.raises(ValueError):
        await plugin.api_get_avatar("qq", "0")
