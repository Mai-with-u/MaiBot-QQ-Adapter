from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pathlib import Path
from tomlkit.exceptions import ParseError
from unittest.mock import AsyncMock, Mock

import asyncio
import base64
import json

import pytest
import tomlkit

from plugins.qq_official_adapter.qr_login import QRLogin, decrypt_secret, save_credentials
from plugins.qq_official_adapter.plugin import QQOfficialAdapterPlugin


def encrypted_secret(key: bytes, secret: str) -> str:
    nonce = b"012345678901"
    return base64.b64encode(nonce + AESGCM(key).encrypt(nonce, secret.encode(), None)).decode()


def test_official_aes_layout_and_tampering() -> None:
    key = bytes(range(32))
    encrypted = encrypted_secret(key, "test-secret")
    assert decrypt_secret(encrypted, key) == "test-secret"
    damaged = bytearray(base64.b64decode(encrypted))
    damaged[-1] ^= 1
    with pytest.raises(InvalidTag):
        decrypt_secret(base64.b64encode(damaged).decode(), key)


def test_save_preserves_config_and_enables_connection(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('# keep comment\n[plugin]\nenabled = false\n[qq_official]\napp_id = "old"\napp_secret = "old-secret"\nunified_account_id = "123"\n[mute]\nenabled = false\n', encoding="utf-8")
    result = save_credentials(path, "456", "new-secret")
    assert result["plugin"]["enabled"] is True
    assert result["qq_official"] == {"app_id": "456", "app_secret": "new-secret", "unified_account_id": "123"}
    assert result["mute"]["enabled"] is False
    assert "# keep comment" in path.read_text(encoding="utf-8")
    assert tomlkit.parse(path.read_text(encoding="utf-8")).unwrap() == result
    assert not list(tmp_path.glob(".qq-login-*"))


def test_malformed_config_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = b"[broken"
    path.write_bytes(original)
    with pytest.raises(ParseError):
        save_credentials(path, "123", "secret")
    assert path.read_bytes() == original


def test_qr_success_does_not_expose_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        apply = AsyncMock()
        login = QRLogin(apply)
        key = b""

        async def post(session, endpoint, payload):
            nonlocal key
            if endpoint == "create_bind_task":
                key = base64.b64decode(payload["key"])
                return {"task_id": "task-123"}
            assert payload == {"task_id": "task-123"}
            assert login.snapshot()["image"].startswith("data:image/png;base64,")
            return {"status": 2, "bot_appid": "123", "bot_encrypt_secret": encrypted_secret(key, "sensitive-secret")}

        monkeypatch.setattr(login, "_post", post)
        assert (await login.start())["state"] == "creating"
        await login._task
        apply.assert_awaited_once_with("123", "sensitive-secret")
        snapshot = login.snapshot()
        assert snapshot["state"] == "success"
        assert snapshot["image"] == ""
        assert "sensitive-secret" not in json.dumps(snapshot)
        assert base64.b64encode(key).decode() not in json.dumps(snapshot)
    asyncio.run(run())


@pytest.mark.parametrize("status, expected", [(3, "expired"), (99, "error")])
def test_expired_and_unknown_state(monkeypatch: pytest.MonkeyPatch, status: int, expected: str) -> None:
    async def run() -> None:
        apply = AsyncMock()
        login = QRLogin(apply)
        monkeypatch.setattr(login, "_post", AsyncMock(side_effect=[{"task_id": "task"}, {"status": status}]))
        await login.start()
        await login._task
        assert login.snapshot()["state"] == expected
        assert login.snapshot()["image"] == ""
        apply.assert_not_awaited()
    asyncio.run(run())


def test_cancel_and_restart_discard_old_task(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        apply = AsyncMock()
        login = QRLogin(apply)
        entered = asyncio.Event()

        async def post(*args):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(login, "_post", post)
        await login.start()
        old = login._task
        await entered.wait()
        await login.start()
        assert old.cancelled()
        await asyncio.sleep(0)
        assert (await login.cancel())["state"] == "cancelled"
        apply.assert_not_awaited()
    asyncio.run(run())


def test_cancel_waits_for_in_progress_save(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        saving, finish = asyncio.Event(), asyncio.Event()

        async def apply(app_id, secret):
            saving.set()
            await finish.wait()

        login = QRLogin(apply)
        key = bytes(range(32))
        monkeypatch.setattr("plugins.qq_official_adapter.qr_login.os.urandom", lambda size: key)
        monkeypatch.setattr(login, "_post", AsyncMock(side_effect=[{"task_id": "task"}, {"status": 2, "bot_appid": "123", "bot_encrypt_secret": encrypted_secret(key, "secret")}]))
        await login.start()
        await saving.wait()
        cancellation = asyncio.create_task(login.cancel())
        await asyncio.sleep(0)
        assert not cancellation.done()
        finish.set()
        assert (await cancellation)["state"] == "success"
    asyncio.run(run())


def test_plugin_applies_saved_login_without_exposing_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        path = tmp_path / "config.toml"
        path.write_text('[plugin]\nenabled = false\nconfig_version = "0.4.1"\n[qq_official]\napp_id = "old"\napp_secret = "old-secret"\n', encoding="utf-8")
        plugin = QQOfficialAdapterPlugin()
        plugin.set_plugin_config(plugin.get_default_config())
        stop, start = AsyncMock(), Mock()
        monkeypatch.setattr(plugin, "_stop", stop)
        monkeypatch.setattr(plugin, "_start", start)
        monkeypatch.setattr("plugins.qq_official_adapter.plugin.save_credentials", lambda _, app_id, secret: save_credentials(path, app_id, secret))
        await plugin._apply_qr_credentials("123", "private-secret")
        stop.assert_awaited_once()
        start.assert_called_once()
        assert plugin.config.plugin.enabled
        assert plugin.config.qq_official.app_secret == "private-secret"
        status = await plugin.webui_login_status()
        assert status["configured_app_id"] == "123"
        assert not status["connected"]  # 保存成功与实际网关连接分开显示。
        assert "private-secret" not in json.dumps(status)
    asyncio.run(run())
