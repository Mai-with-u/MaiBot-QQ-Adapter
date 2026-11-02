"""Narrow regressions for the two selectively ported upstream fixes; no network."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List, Mapping
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.qq_official_adapter.plugin import QQOfficialAdapterPlugin


@pytest.mark.parametrize("host", ["ugcimg.cn", "gchat.qpic.ugcimg.cn", "GCHAT.QPIC.UGCIMG.CN"])
def test_voice_media_domain_allowed(host: str) -> None:
    QQOfficialAdapterPlugin._validate_media_url(f"https://{host}/synthetic-audio")


@pytest.mark.parametrize("url", [
    "https://notugcimg.cn/audio", "https://ugcimg.cn.example.org/audio",
    "http://ugcimg.cn/audio", "https://ugcimg.cn:8443/audio",
    "https://user:password@ugcimg.cn/audio",
])
def test_voice_media_domain_does_not_relax_url_validation(url: str) -> None:
    with pytest.raises(RuntimeError):
        QQOfficialAdapterPlugin._validate_media_url(url)


@pytest.mark.asyncio
@pytest.mark.parametrize("event,endpoint", [
    ("GROUP_MESSAGE_CREATE", "/v2/groups/group-openid/messages"),
    ("C2C_MESSAGE_CREATE", "/v2/users/user-openid/messages"),
])
@pytest.mark.parametrize("ext,expected", [
    (["msg_idx=command-index==", "ref_msg_idx=older-index"], "command-index=="),
    (["ref_msg_idx=older-index"], None),
    (["msg_idx=command-index", "msg_idx=conflicting-index"], None),
    (["msg_idx=invalid index"], None),
    ([], None),
])
async def test_command_receipt_visual_reference(
    event: str, endpoint: str, ext: List[str], expected: str | None,
) -> None:
    plugin = QQOfficialAdapterPlugin()
    plugin.set_plugin_config(plugin.get_default_config())
    plugin._set_context(SimpleNamespace(logger=Mock()))
    plugin._authorize = AsyncMock(return_value="synthetic-token")
    bodies = []

    @asynccontextmanager
    async def post(url: str, *, headers: Mapping[str, str], json: Dict[str, Any]) -> AsyncIterator[Any]:
        assert url.endswith(endpoint)
        bodies.append(json)
        yield SimpleNamespace(status=200)

    plugin._session = SimpleNamespace(closed=False, post=post)
    data = {"id": "command-id", "group_openid": "group-openid",
            "author": {"user_openid": "user-openid"}, "message_scene": {"ext": ext}}
    await plugin._reply_to_command(event, data, "synthetic-result")
    body = {"content": "synthetic-result", "msg_type": 0, "msg_id": "command-id", "msg_seq": 1}
    if expected is not None:
        body["message_reference"] = {"message_id": expected}
    assert bodies == [body]
