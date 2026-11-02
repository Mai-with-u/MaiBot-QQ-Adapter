from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock, Mock

import copy
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time

import pytest

from plugins.qq_official_adapter import reference_debug as debug
from test_message_references import GROUP_EVENT, SCOPE, inbound, make_plugin


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> debug.ReferenceDebug:
    monkeypatch.setenv("QQ_REFERENCE_DEBUG_SALT", "test-shared-salt-not-for-production-0001")
    monkeypatch.setenv("QQ_REFERENCE_DEBUG_UNTIL", str(time.time() + 600))
    probe = debug.ReferenceDebug()
    monkeypatch.setattr("plugins.qq_official_adapter.plugin.probe", probe)
    return probe


def records(ctx: Any) -> List[Dict[str, Any]]:
    return [json.loads(call.args[1]) for call in ctx.logger.info.call_args_list
            if call.args[0] == "[QQREF-DEBUG-v1] %s"]


def test_default_disabled_does_not_inspect_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QQ_REFERENCE_DEBUG_SALT", raising=False)
    monkeypatch.delenv("QQ_REFERENCE_DEBUG_UNTIL", raising=False)
    probe = debug.ReferenceDebug()
    probe.summary = Mock(side_effect=AssertionError("must not inspect"))
    probe.emit(None, "test", {})
    assert not probe.active()
    probe.summary.assert_not_called()


@pytest.mark.parametrize("until,salt", [("bad", "x" * 32), ("nan", "x" * 32),
                                      ("inf", "x" * 32), ("0", "x" * 32), ("9999999999", "short")])
def test_invalid_opt_in_is_silent(monkeypatch: pytest.MonkeyPatch, until: str, salt: str) -> None:
    monkeypatch.setenv("QQ_REFERENCE_DEBUG_UNTIL", until)
    monkeypatch.setenv("QQ_REFERENCE_DEBUG_SALT", salt)
    assert not debug.ReferenceDebug().active()


def test_bounded_private_and_non_mutating(enabled: debug.ReferenceDebug) -> None:
    ctx = SimpleNamespace(logger=Mock())
    message = {"message_id": "raw-message-secret", "raw_message": [
        {"type": "reply", "data": {"target_message_id": "raw-target-secret"}},
        {"type": "image", "binary_data_base64": "base64-secret", "data": "https://signed-secret"},
        {"type": "text", "data": "body-secret"}, {"type": "unknown-secret"},
    ]}
    original = copy.deepcopy(message)
    for _ in range(250):
        enabled.emit(ctx, "test", message, target="raw-target-secret", unsafe="QQ-Token-secret")
    output = str(ctx.logger.info.call_args_list)
    for secret in ("raw-message-secret", "raw-target-secret", "base64-secret", "signed-secret",
                   "body-secret", "unknown-secret", "QQ-Token-secret"):
        assert secret not in output
    assert len(records(ctx)) == 200
    assert not enabled.active()
    first = records(ctx)[0]
    assert first["target"] == first["targets"][0]
    assert first["counts"]["binary"] == 1
    assert message == original


def test_expiration_and_hard_duration_cap(enabled: debug.ReferenceDebug, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QQ_REFERENCE_DEBUG_UNTIL", str(time.time() + 999999))
    capped = debug.ReferenceDebug()
    assert capped._deadline - time.monotonic() <= 900
    monkeypatch.setattr(debug.time, "monotonic", lambda: capped._deadline + 1)
    assert not capped.active()
    assert not enabled.active()


def test_wall_deadline_and_logger_failure(enabled: debug.ReferenceDebug, monkeypatch: pytest.MonkeyPatch) -> None:
    enabled.emit(SimpleNamespace(logger=Mock(info=Mock(side_effect=RuntimeError("logger failed")))), "test", {})
    monkeypatch.setattr(debug.time, "time", lambda: enabled._until + 1)
    assert not enabled.active()


def test_shared_salt_correlates_in_separate_process(enabled: debug.ReferenceDebug) -> None:
    root = Path(__file__).resolve().parents[1]
    script = (
        "import runpy; runpy.run_path('tests/conftest.py'); "
        "from plugins.qq_official_adapter.reference_debug import probe; print(probe.token('message-B'))"
    )
    remote = subprocess.check_output([sys.executable, "-c", script], cwd=root, env=os.environ, text=True).strip()
    expected = hmac.new(os.environ["QQ_REFERENCE_DEBUG_SALT"].encode(),
                        b"qq-reference-message-v1\0message-B", hashlib.sha256).hexdigest()[:24]
    assert enabled.token("message-B") == remote == expected


@pytest.mark.parametrize("state", ["hit", "miss", "quarantine", "malformed", "missing", "self"])
@pytest.mark.asyncio
async def test_actual_adapter_probe_branches(enabled: debug.ReferenceDebug, state: str) -> None:
    plugin = make_plugin()
    await plugin._set_ready(True)
    plugin._references.remember(SCOPE, "old-message", "old-index")
    data = inbound()
    if state == "miss":
        data["message_scene"]["ext"] = ["ref_msg_idx=unknown"]
    elif state == "quarantine":
        plugin._references.remember(SCOPE, "conflict", "old-index")
    elif state == "malformed":
        data["message_scene"]["ext"].append("ref_msg_idx=duplicate")
    elif state == "missing":
        del data["message_scene"]
    elif state == "self":
        data["message_scene"]["ext"] = ["msg_idx=current-index", "ref_msg_idx=current-index"]
    await plugin._receive_message(GROUP_EVENT, data, account_id="bot", bot_name="Bot")
    routed = plugin.ctx.gateway.route_message.call_args.kwargs["message"]
    before, after = records(plugin.ctx)
    assert before["stage"] == "adapter.route" and after["stage"] == "adapter.routed"
    assert after["accepted"] is True
    assert before["counts"]["reply"] == (state == "hit")
    assert before["quarantined"] == (state == "quarantine")
    assert before["ext_valid"] == (state not in {"malformed", "missing"})
    assert before["self_target"] == (state == "self")
    assert sum(x["type"] == "reply" for x in routed["raw_message"]) == (state == "hit")


@pytest.mark.asyncio
async def test_b_own_image_and_c_target_correlate(enabled: debug.ReferenceDebug) -> None:
    plugin = make_plugin()
    await plugin._set_ready(True)
    plugin._references.remember(SCOPE, "message-A", "index-A")
    plugin._download_attachment = AsyncMock(return_value=b"test-image-content")
    b = inbound("message-B", "index-B", "index-A")
    b["attachments"] = [{"content_type": "image/png", "url": "https://grouppro.grouppro.qq.com/private-url"}]
    await plugin._receive_message(GROUP_EVENT, b, account_id="bot", bot_name="Bot")
    await plugin._receive_message(GROUP_EVENT, inbound("message-C", "index-C", "index-B"),
                                  account_id="bot", bot_name="Bot")
    b_log, _, c_log, _ = records(plugin.ctx)
    assert b_log["counts"]["reply"] == b_log["counts"]["image"] == b_log["counts"]["binary"] == 1
    assert c_log["target"] == c_log["targets"][0] == b_log["mid"]
    assert c_log["counts"]["image"] == 0
    assert b_log["target"] == enabled.token("message-A")


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostics", [False, True])
async def test_route_exception_unchanged(enabled: debug.ReferenceDebug, diagnostics: bool) -> None:
    enabled._enabled = diagnostics
    plugin = make_plugin()
    await plugin._set_ready(True)
    original = RuntimeError("gateway failure")
    plugin.ctx.gateway.route_message.side_effect = original
    plugin.ctx.logger.info.side_effect = RuntimeError("logger failure")
    with pytest.raises(RuntimeError) as caught:
        await plugin._receive_message(GROUP_EVENT, inbound(), account_id="bot", bot_name="Bot")
    assert caught.value is original
    assert not plugin._replies
