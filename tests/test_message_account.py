import pytest

from codec import decode_message


@pytest.mark.parametrize("event_type", ["C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"])
@pytest.mark.parametrize("unified", ["", "mapped-bot"])
def test_official_message_routes_with_formal_account(event_type: str, unified: str) -> None:
    is_group = event_type != "C2C_MESSAGE_CREATE"
    payload = {
        "id": "msg",
        "author": {"member_openid" if is_group else "user_openid": "user"},
        "content": "你好",
        "timestamp": "2026-10-09T00:00:00Z",
    }
    if is_group:
        payload["group_openid"] = "group"

    message, scene, target = decode_message(
        event_type,
        payload,
        "official-bot",
        id_map={"user": {"user": "mapped-user"}, "group": {"group": "mapped-group"}},
        unified_account_id=unified,
    )
    assert message["account_id"] == (unified or "official-bot")
    assert message["scope"] is None
    assert "self_id" not in message["message_info"]["additional_config"]
    assert "platform_io_account_id" not in message["message_info"]["additional_config"]
    assert message["message_info"]["user_info"]["user_id"] == "mapped-user"
    assert message["message_info"]["additional_config"]["qq_official_user_openid"] == "user"
    assert scene == ("group" if is_group else "private")
    assert target == ("mapped-group" if is_group else "mapped-user")
    if is_group:
        assert message["message_info"]["group_info"]["group_id"] == "mapped-group"
        assert message["message_info"]["additional_config"]["qq_official_group_openid"] == "group"
