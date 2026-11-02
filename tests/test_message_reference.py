from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.qq_official_adapter.codec import decode_message, encode_message
from plugins.qq_official_adapter.plugin import QQOfficialAdapterPlugin
from plugins.qq_official_adapter.references import (
    REFERENCE_CAPACITY, REFERENCE_TTL, ReferenceCache, parse_scene_indexes, reply_target,
)

SCOPE = ("bot-openid", "group", "group-openid")


def _make_plugin() -> QQOfficialAdapterPlugin:
    """保留上游引用回归，改用真实 SDK 初始化及双向缓存。"""
    plugin = QQOfficialAdapterPlugin()
    plugin.set_plugin_config(plugin.get_default_config())
    plugin._set_context(SimpleNamespace(logger=Mock()))
    plugin._account_id = SCOPE[0]
    return plugin


def test_extract_message_scene_fields() -> None:
    payload = {
        "message_scene": {
            "source": "default",
            "ext": [
                "msg_idx=REFIDX_abc123==",
                "auth_token=tok",
                "ref_msg_idx=TMP_xxxxxxxx-xxxx",
            ],
        }
    }
    msg_idx, ref_msg_idx = parse_scene_indexes(payload)
    assert msg_idx == "REFIDX_abc123=="
    assert ref_msg_idx == "TMP_xxxxxxxx-xxxx"


def test_extract_message_scene_fields_without_scene() -> None:
    assert parse_scene_indexes({}) == ("", "")
    assert parse_scene_indexes({"message_scene": {"source": "default"}}) == ("", "")


def test_decode_message_keeps_indexes_in_scoped_cache_not_additional_config() -> None:
    message, _, _ = decode_message(
        "GROUP_MESSAGE_CREATE",
        {
            "id": "ROBOT1.0_inbound",
            "author": {"member_openid": "user-openid"},
            "content": "你好",
            "group_openid": "group-openid",
            "timestamp": "2026-10-27T00:00:00Z",
            "message_scene": {"source": "default", "ext": ["msg_idx=REFIDX_inbound==", "auth_token=tok"]},
        },
        "bot-openid",
    )
    config = message["message_info"]["additional_config"]
    assert "qq_official_msg_idx" not in config
    assert "qq_official_ref_msg_idx" not in config
    plugin = _make_plugin()
    plugin._observe_reference("GROUP_MESSAGE_CREATE", {
        "id": "ROBOT1.0_inbound", "group_openid": SCOPE[2],
        "message_scene": {"ext": ["msg_idx=REFIDX_inbound=="]},
    }, SCOPE[0])
    assert plugin._references.index_for(SCOPE, message["message_id"]) == "REFIDX_inbound=="
    assert plugin._references.id_for(SCOPE, "REFIDX_inbound==") == message["message_id"]


def test_encode_message_keeps_body_but_legacy_string_reply_has_no_visual_target() -> None:
    message = {
        "message_info": {
            "group_info": {"group_id": "group-openid"},
            "additional_config": {},
        },
        "raw_message": [
            {"type": "reply", "data": "ROBOT1.0_quoted"},
            {"type": "text", "data": "收到"},
        ],
    }
    scene, target, parts = encode_message(message)
    assert scene == "group"
    assert target == "group-openid"
    # 明确契约变化：仅标准SDK reply可产生可视引用，旧字符串不再接受。
    assert reply_target(message) == ""
    # reply 段不进入正文，正文只保留文本。
    assert parts == [{"kind": "body", "body": {"msg_type": 0, "content": "收到"}}]


def test_encode_message_accepts_reply_segment_dict_data() -> None:
    message = {
        "message_info": {
            "group_info": {"group_id": "group-openid"},
            "additional_config": {},
        },
        "raw_message": [
            {"type": "reply", "data": {"target_message_id": "ROBOT1.0_quoted2"}},
            {"type": "text", "data": "嗯"},
        ],
    }
    encode_message(message)
    assert reply_target(message) == "ROBOT1.0_quoted2"


def test_encode_message_without_reply_returns_empty_target() -> None:
    message = {
        "message_info": {
            "group_info": {"group_id": "group-openid"},
            "additional_config": {},
        },
        "raw_message": [{"type": "text", "data": "你好"}],
    }
    _, _, parts = encode_message(message)
    assert reply_target(message) == ""
    assert parts == [{"kind": "body", "body": {"msg_type": 0, "content": "你好"}}]


def test_ref_idx_store_lookup_and_message_reference() -> None:
    plugin = _make_plugin()
    plugin._references.remember(SCOPE, "ROBOT1.0_1", "REFIDX_1==")
    assert plugin._references.index_for(SCOPE, "ROBOT1.0_1") == "REFIDX_1=="
    assert plugin._references.id_for(SCOPE, "REFIDX_1==") == "ROBOT1.0_1"
    # 查不到的消息按无引用降级。
    assert plugin._references.index_for(SCOPE, "ROBOT1.0_missing") == ""


@pytest.mark.asyncio
async def test_store_send_ref_idx_from_ext_info() -> None:
    plugin = _make_plugin()
    plugin._ready = True
    plugin._api_post = AsyncMock(return_value=
        {"id": "ROBOT1.0_bot", "timestamp": "2026-10-27T10:00:00+08:00", "ext_info": {"ref_idx": "REFIDX_bot=="}}
    )
    message = {"message_info": {"group_info": {"group_id": SCOPE[2]}},
               "raw_message": [{"type": "text", "data": "synthetic-body"}]}
    assert (await plugin.handle_qq_official_gateway(message))["success"]
    assert plugin._references.index_for(SCOPE, "ROBOT1.0_bot") == "REFIDX_bot=="
    # 无 ext_info 时不应产生映射。
    plugin._api_post = AsyncMock(return_value={"id": "ROBOT1.0_noext"})
    assert (await plugin.handle_qq_official_gateway(message))["success"]
    assert plugin._references.index_for(SCOPE, "ROBOT1.0_noext") == ""


def test_ref_idx_expires_after_ttl() -> None:
    plugin = _make_plugin()
    now = [0.0]
    plugin._references = ReferenceCache(clock=lambda: now[0])
    plugin._references.remember(SCOPE, "ROBOT1.0_old", "REFIDX_old==")
    now[0] = REFERENCE_TTL + 1
    assert plugin._references.index_for(SCOPE, "ROBOT1.0_old") == ""


def test_ref_idx_respects_cap() -> None:
    plugin = _make_plugin()
    for index in range(REFERENCE_CAPACITY + 50):
        plugin._references.remember(SCOPE, f"ROBOT1.0_{index}", f"REFIDX_{index}==")
    assert len(plugin._references._forward) == REFERENCE_CAPACITY
    # 最早插入的条目应被淘汰。
    assert plugin._references.index_for(SCOPE, "ROBOT1.0_0") == ""
    assert plugin._references.index_for(SCOPE, f"ROBOT1.0_{REFERENCE_CAPACITY + 49}") == f"REFIDX_{REFERENCE_CAPACITY + 49}=="
