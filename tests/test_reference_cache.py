from typing import Any

import pytest

from plugins.qq_official_adapter.references import ReferenceCache, Scope, parse_scene_indexes, reply_target

SCOPE = ("actual-bot", "group", "raw-target")


def test_padding_exact_lookup_and_no_prefix_guess() -> None:
    assert parse_scene_indexes({"message_scene": {"ext": ["msg_idx=REFIDX_YQ==", "ref_msg_idx=TMP_YQ=="]}}) == (
        "REFIDX_YQ==", "TMP_YQ==")
    cache = ReferenceCache()
    assert cache.remember(SCOPE, "message", "REFIDX_YQ==")
    assert cache.id_for(SCOPE, "REFIDX_YQ==") == "message"
    assert cache.id_for(SCOPE, "TMP_YQ==") == ""
    assert cache.index_for(SCOPE, "message") == "REFIDX_YQ=="


@pytest.mark.parametrize("ext", [None, {}, "msg_idx=x", [None], [1], ["missing-equals"],
    ["msg_idx="], ["msg_idx=white space"], ["msg_idx=\n"], ["msg_idx=中文"],
    ["msg_idx=" + "x" * 2049], ["msg_idx=x", "msg_idx=x"], ["msg_idx=x", "msg_idx=y"],
    ["msg_idx=x", "ref_msg_idx=a", "ref_msg_idx=b"], ["invalid-key=x"],
    ["auth_token=x", "auth_token=y", "msg_idx=z"], ["x=y"] * 33])
def test_malformed_ext_is_unparseable(ext: Any) -> None:
    assert parse_scene_indexes({"message_scene": {"ext": ext}}) == ("", "")


@pytest.mark.parametrize("scene", [None, 1, [], {}, {"ext": []}])
def test_missing_scene(scene: Any) -> None:
    assert parse_scene_indexes({"message_scene": scene}) == ("", "")


@pytest.mark.parametrize("scope", [("other-bot", "group", "raw-target"),
    ("actual-bot", "private", "raw-target"), ("actual-bot", "group", "other-target")])
def test_scope_isolation(scope: Scope) -> None:
    cache = ReferenceCache()
    cache.remember(SCOPE, "message", "idx")
    assert cache.index_for(scope, "message") == ""
    assert cache.id_for(scope, "idx") == ""
    assert cache.remember(scope, "message", "other-idx")
    assert cache.index_for(SCOPE, "message") == "idx"


@pytest.mark.parametrize("message_id, index", [("message", "different"), ("different", "idx")])
def test_conflict_quarantines_scope_and_replay_cannot_restore(message_id: str, index: str) -> None:
    cache = ReferenceCache()
    cache.remember(SCOPE, "message", "idx")
    cache.remember(SCOPE, "another", "another-idx")
    assert not cache.remember(SCOPE, message_id, index)
    assert not cache.remember(SCOPE, "message", "idx")
    assert cache.index_for(SCOPE, "message") == ""
    assert cache.id_for(SCOPE, "idx") == ""
    assert cache.id_for(SCOPE, "another-idx") == ""
    assert len(cache._forward) == 1
    assert not cache._reverse


def test_ttl_capacity_and_duplicates_do_not_extend_retention() -> None:
    now = [0.0]
    cache = ReferenceCache(capacity=2, ttl=10, clock=lambda: now[0])
    cache.remember(SCOPE, "a", "ia")
    now[0] = 1
    cache.remember(SCOPE, "b", "ib")
    now[0] = 9
    assert cache.remember(SCOPE, "a", "ia")
    now[0] = 10
    assert cache.index_for(SCOPE, "a") == cache.id_for(SCOPE, "ia") == ""
    cache.remember(SCOPE, "c", "ic")
    cache.remember(SCOPE, "d", "id")
    assert cache.id_for(SCOPE, "ib") == cache.index_for(SCOPE, "b") == ""
    assert len(cache._reverse) == len(cache._forward) == 2
    now[0] = 20
    cache.prune()
    assert not cache._forward and not cache._reverse


def test_quarantine_expires_and_clear_removes_both_directions() -> None:
    now = [0.0]
    cache = ReferenceCache(ttl=10, clock=lambda: now[0])
    cache.remember(SCOPE, "a", "ia")
    cache.remember(SCOPE, "a", "ib")
    now[0] = 10
    assert cache.remember(SCOPE, "a", "ia")
    cache.clear()
    assert cache.id_for(SCOPE, "ia") == cache.index_for(SCOPE, "a") == ""


def test_capacity_evicts_quarantine_as_one_slot_and_keeps_other_scopes() -> None:
    cache = ReferenceCache(capacity=2)
    other = ("other-bot", "private", "other-target")
    cache.remember(SCOPE, "a", "ia")
    cache.remember(SCOPE, "a", "conflict")
    cache.remember(other, "a", "ia")
    assert cache.id_for(other, "ia") == "a"
    cache.remember(other, "b", "ib")
    assert (SCOPE, "") not in cache._forward
    assert len(cache._forward) == len(cache._reverse) == 2


@pytest.mark.parametrize("scope", [("", "group", "target"), ("bot", "channel", "target"),
    ("bot", "private", ""), ("bot", "group", "bad target")])
def test_invalid_scope_is_not_cached(scope: Scope) -> None:
    assert not ReferenceCache().remember(scope, "a", "i")


@pytest.mark.parametrize("bad", [None, 1, [], {}, "", " white", "bad\t", "x" * 2049])
def test_invalid_values_do_not_enter_cache(bad: Any) -> None:
    cache = ReferenceCache()
    assert not cache.remember(SCOPE, bad, "idx")
    assert not cache.remember(SCOPE, "message", bad)
    assert cache.index_for(SCOPE, bad) == cache.id_for(SCOPE, bad) == ""


@pytest.mark.parametrize("data", ["id", {}, {"message_id": "id"}, {"target_message_id": 1}])
def test_only_standard_reply_target(data: Any) -> None:
    assert reply_target({"raw_message": [{"type": "reply", "data": data}]}) == ""


def test_standard_reply_and_ambiguous_reply() -> None:
    segment = {"type": "reply", "data": {"target_message_id": "target"}}
    assert reply_target({"raw_message": [segment]}) == "target"
    assert reply_target({"raw_message": [segment, segment]}) == ""
