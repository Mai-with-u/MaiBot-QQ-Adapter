import time

from qq_official_adapter.codec import decode_message, encode_message, extract_message_scene_fields
from qq_official_adapter.plugin import MAX_REF_IDX_ENTRIES, REF_IDX_TTL_SEC, QQOfficialAdapterPlugin


def _make_plugin() -> QQOfficialAdapterPlugin:
    """绕过 SDK 初始化构造插件实例，仅测试引用索引相关的同步方法。"""
    plugin = QQOfficialAdapterPlugin.__new__(QQOfficialAdapterPlugin)
    plugin._ref_idx = {}
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
    msg_idx, ref_msg_idx = extract_message_scene_fields(payload)
    assert msg_idx == "REFIDX_abc123=="
    assert ref_msg_idx == "TMP_xxxxxxxx-xxxx"


def test_extract_message_scene_fields_without_scene() -> None:
    assert extract_message_scene_fields({}) == ("", "")
    assert extract_message_scene_fields({"message_scene": {"source": "default"}}) == ("", "")


def test_decode_message_stores_msg_idx_in_additional_config() -> None:
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
    assert config["qq_official_msg_idx"] == "REFIDX_inbound=="
    assert "qq_official_ref_msg_idx" not in config


def test_encode_message_captures_reply_target_instead_of_dropping() -> None:
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
    scene, target, parts, reply_target_id = encode_message(message)
    assert scene == "group"
    assert target == "group-openid"
    assert reply_target_id == "ROBOT1.0_quoted"
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
    _, _, _, reply_target_id = encode_message(message)
    assert reply_target_id == "ROBOT1.0_quoted2"


def test_encode_message_without_reply_returns_empty_target() -> None:
    message = {
        "message_info": {
            "group_info": {"group_id": "group-openid"},
            "additional_config": {},
        },
        "raw_message": [{"type": "text", "data": "你好"}],
    }
    _, _, parts, reply_target_id = encode_message(message)
    assert reply_target_id == ""
    assert parts == [{"kind": "body", "body": {"msg_type": 0, "content": "你好"}}]


def test_ref_idx_store_lookup_and_message_reference() -> None:
    plugin = _make_plugin()
    plugin._store_ref_idx("ROBOT1.0_1", "REFIDX_1==")
    assert plugin._lookup_ref_idx("ROBOT1.0_1") == "REFIDX_1=="
    assert plugin._build_message_reference("ROBOT1.0_1") == {
        "message_reference": {"message_id": "REFIDX_1=="}
    }
    # 查不到的消息按无引用降级。
    assert plugin._build_message_reference("ROBOT1.0_missing") == {}


def test_store_send_ref_idx_from_ext_info() -> None:
    plugin = _make_plugin()
    plugin._store_send_ref_idx(
        {"id": "ROBOT1.0_bot", "timestamp": "2026-10-27T10:00:00+08:00", "ext_info": {"ref_idx": "REFIDX_bot=="}}
    )
    assert plugin._lookup_ref_idx("ROBOT1.0_bot") == "REFIDX_bot=="
    # 无 ext_info 时不应产生映射。
    plugin._store_send_ref_idx({"id": "ROBOT1.0_noext"})
    assert plugin._lookup_ref_idx("ROBOT1.0_noext") == ""


def test_ref_idx_expires_after_ttl() -> None:
    plugin = _make_plugin()
    plugin._store_ref_idx("ROBOT1.0_old", "REFIDX_old==", received_at=time.time() - REF_IDX_TTL_SEC - 1)
    assert plugin._lookup_ref_idx("ROBOT1.0_old") == ""


def test_ref_idx_respects_cap() -> None:
    plugin = _make_plugin()
    for index in range(MAX_REF_IDX_ENTRIES + 50):
        plugin._store_ref_idx(f"ROBOT1.0_{index}", f"REFIDX_{index}==")
    assert len(plugin._ref_idx) == MAX_REF_IDX_ENTRIES
    # 最早插入的条目应被淘汰。
    assert plugin._lookup_ref_idx("ROBOT1.0_0") == ""
    assert plugin._lookup_ref_idx(f"ROBOT1.0_{MAX_REF_IDX_ENTRIES + 49}") == f"REFIDX_{MAX_REF_IDX_ENTRIES + 49}=="
