"""指令菜单下发逻辑的单元测试，全部走打桩的平台接口调用。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import Mock

import pytest

from qq_official_adapter.plugin import QQOfficialAdapterPlugin


class _Logger:
    def debug(self, *args: Any, **kwargs: Any) -> None:
        pass

    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def warning(self, *args: Any, **kwargs: Any) -> None:
        pass


class _Ctx:
    def __init__(self) -> None:
        self.logger = _Logger()


class _ApiStub:
    """记录出站调用并按约定返回预置响应。"""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Optional[Dict[str, Any]]]] = []
        self.menu: Dict[str, Any] = {}
        self.panels: List[Dict[str, Any]] = []

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.calls.append(("GET", path, dict(params or {})))
        if path == "/v2/menu":
            return self.menu
        return {"records": list(self.panels), "is_end": True}

    async def post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(("POST", path, dict(payload)))
        return {}

    async def put(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(("PUT", path, dict(payload)))
        return {}

    async def delete(self, path: str) -> Dict[str, Any]:
        self.calls.append(("DELETE", path, None))
        return {}

    def writes(self) -> List[Tuple[str, str, Optional[Dict[str, Any]]]]:
        return [call for call in self.calls if call[0] != "GET"]

    def payload_of(self, method: str, path: str) -> Dict[str, Any]:
        for recorded_method, recorded_path, payload in self.calls:
            if recorded_method == method and recorded_path == path:
                return dict(payload or {})
        raise AssertionError(f"未找到调用: {method} {path}")


def _make_plugin(menu: Dict[str, Any]) -> Tuple[QQOfficialAdapterPlugin, _ApiStub]:
    plugin = QQOfficialAdapterPlugin()
    plugin.set_plugin_config(plugin.get_default_config() | {"menu": menu})
    plugin._ready = True
    plugin._ctx = _Ctx()
    stub = _ApiStub()
    plugin._api_get = stub.get
    plugin._api_post = stub.post
    plugin._api_put = stub.put
    plugin._api_delete = stub.delete
    return plugin, stub


@pytest.mark.asyncio
async def test_private_menu_pushed_from_config():
    plugin, stub = _make_plugin(
        {
            "private_enabled": True,
            "private_items": [
                {"name": "帮助", "type": "send_message", "value": "/help"},
                {"name": "官网", "type": "link", "value": "https://example.com"},
            ],
        }
    )
    await plugin._sync_menus(force=True)
    payload = stub.payload_of("PUT", "/v2/menu")
    assert payload["menu"]["items"] == [
        {"name": "帮助", "type": "send_message", "send_message": "/help"},
        {"name": "官网", "type": "link", "link": "https://example.com"},
    ]
    assert not plugin._menu_failed


@pytest.mark.asyncio
async def test_private_menu_with_sub_items_is_pushed_as_menu():
    plugin, stub = _make_plugin(
        {
            "private_enabled": True,
            "private_items": [
                {"name": "更多", "type": "menu", "sub_items": ["/help", "/status"]},
            ],
        }
    )
    await plugin._sync_menus(force=True)
    items = stub.payload_of("PUT", "/v2/menu")["menu"]["items"]
    assert items[0]["type"] == "menu"
    assert [sub["send_message"] for sub in items[0]["sub_menu_items"]] == ["/help", "/status"]


@pytest.mark.asyncio
async def test_private_menu_skips_write_when_unchanged():
    plugin, stub = _make_plugin(
        {"private_enabled": True, "private_items": [{"name": "帮助", "value": "/help"}]}
    )
    stub.menu = {"menu": {"items": [{"name": "帮助", "type": "send_message", "send_message": "/help"}]}}
    await plugin._sync_menus(force=True)
    assert stub.writes() == []


@pytest.mark.asyncio
async def test_private_menu_cleared_when_list_is_empty():
    plugin, stub = _make_plugin({"private_enabled": True, "private_items": []})
    stub.menu = {"menu": {"items": [{"name": "旧菜单", "type": "send_message", "send_message": "/old"}]}}
    await plugin._sync_menus(force=True)
    assert stub.payload_of("PUT", "/v2/menu")["menu"]["items"] == []


@pytest.mark.asyncio
async def test_group_panel_created_when_missing():
    plugin, stub = _make_plugin(
        {
            "group_enabled": True,
            "group_items": [{"name": "/help", "description": "查看帮助", "only_admin": True}],
        }
    )
    await plugin._sync_menus(force=True)
    payload = stub.payload_of("POST", "/v2/panels")
    assert payload["scope"] == "group"
    assert payload["target_type"] == "all"
    assert payload["panel"]["items"] == [
        {"name": "/help", "desc": "查看帮助", "type": "command", "only_admin": True}
    ]


@pytest.mark.asyncio
async def test_group_panel_updated_when_changed():
    plugin, stub = _make_plugin(
        {"group_enabled": True, "group_items": [{"name": "/help", "description": "查看帮助"}]}
    )
    stub.panels = [
        {
            "panel_id": "PANEL1",
            "target_type": "all",
            "panel": {"items": [{"name": "/old", "desc": "", "type": "command", "only_admin": False}]},
        }
    ]
    await plugin._sync_menus(force=True)
    payload = stub.payload_of("PUT", "/v2/panels/PANEL1")
    assert payload["panel"]["items"][0]["name"] == "/help"
    assert stub.writes() == [("PUT", "/v2/panels/PANEL1", payload)]


@pytest.mark.asyncio
async def test_group_panel_removed_when_list_is_empty():
    plugin, stub = _make_plugin({"group_enabled": True, "group_items": []})
    stub.panels = [{"panel_id": "PANEL1", "target_type": "all", "panel": {"items": [{"name": "/old"}]}}]
    await plugin._sync_menus(force=True)
    assert stub.writes() == [("DELETE", "/v2/panels/PANEL1", None)]


@pytest.mark.asyncio
async def test_disabled_menu_sends_no_request():
    plugin, stub = _make_plugin({})
    await plugin._sync_menus(force=True)
    assert stub.calls == []
    assert "未接管" in plugin._menu_idle_text()


@pytest.mark.asyncio
async def test_invalid_items_are_skipped():
    plugin, stub = _make_plugin(
        {
            "private_enabled": True,
            "private_items": [
                {"name": "裸链接", "type": "link", "value": "http://example.com"},
                {"name": "空指令", "type": "send_message", "value": ""},
                {"name": "帮助", "value": "/help"},
            ],
            "group_enabled": True,
            "group_items": [{"name": "面板裸链接", "type": "link", "link": "ftp://example.com"}],
        }
    )
    await plugin._sync_menus(force=True)
    items = stub.payload_of("PUT", "/v2/menu")["menu"]["items"]
    assert [item["name"] for item in items] == ["帮助"]
    # 群聊面板过滤后为空，视为清空
    assert [call[0] for call in stub.writes()] == ["PUT"]


@pytest.mark.asyncio
async def test_menu_sync_reports_failure_without_raising():
    plugin, stub = _make_plugin({"private_enabled": True, "private_items": [{"name": "帮助", "value": "/help"}]})

    async def _boom(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raise RuntimeError("QQ 接口 menu 失败: HTTP 403 code=40034105 message=no permission")

    plugin._api_get = _boom
    await plugin._sync_menus(force=True)
    assert plugin._menu_failed
    assert "40034105" in plugin._menu_message
    assert plugin._menu_synced_signature == ""


@pytest.mark.asyncio
async def test_unchanged_signature_skips_second_sync():
    plugin, stub = _make_plugin({"private_enabled": True, "private_items": [{"name": "帮助", "value": "/help"}]})
    await plugin._sync_menus(force=True)
    assert len(stub.calls) == 2
    await plugin._sync_menus()
    assert len(stub.calls) == 2


@pytest.mark.asyncio
async def test_widget_action_triggers_sync():
    plugin, stub = _make_plugin({"private_enabled": True, "private_items": [{"name": "帮助", "value": "/help"}]})
    plugin._qr_login = Mock()
    await plugin.on_config_widget_action("menu_sync")
    assert ("PUT", "/v2/menu") in [(method, path) for method, path, _ in stub.calls]


@pytest.mark.asyncio
async def test_config_widget_state_covers_menu_widgets():
    from maibot_sdk.config_widgets import ConfigWidgetSnapshot

    plugin, _ = _make_plugin({})
    states = await plugin.get_config_widget_state()
    snapshot = ConfigWidgetSnapshot(widgets=plugin.config_widgets, states=states)
    assert "menu" in snapshot.states
    assert states["menu_sync"].disabled


def test_config_schema_exposes_item_limits_to_frontend():
    from maibot_sdk.config import generate_plugin_config_schema

    from qq_official_adapter.settings import AdapterSettings

    schema = generate_plugin_config_schema(
        AdapterSettings,
        plugin_id="Mai-with-u.qq-adapter",
        plugin_name="QQ 官方机器人适配器",
        plugin_version="0.5.0",
        plugin_description="",
        plugin_author="",
    )
    fields = schema["sections"]["menu"]["fields"]
    assert fields["private_items"]["item_type"] == "object"
    assert fields["private_items"]["max_items"] == 10
    assert fields["group_items"]["max_items"] == 20
    assert fields["private_items"]["item_fields"]["type"]["choices"] == [
        "send_message",
        "link",
        "menu",
    ]


def test_over_limit_items_do_not_break_config_validation():
    plugin = QQOfficialAdapterPlugin()
    plugin.set_plugin_config(
        plugin.get_default_config()
        | {
            "menu": {
                "private_enabled": True,
                "private_items": [
                    {"name": f"项{index}", "value": f"/c{index}"} for index in range(11)
                ],
                "group_enabled": True,
                "group_items": [{"name": f"/g{index}"} for index in range(21)],
            }
        }
    )
    # 超出上限由下发时截断兜底，配置本身不能校验失败（否则整份配置报废）
    assert len(plugin.config.menu.private_items) == 11
    assert len(plugin._build_private_menu_items()) == 10
    assert len(plugin._build_group_panel_items()) == 20
