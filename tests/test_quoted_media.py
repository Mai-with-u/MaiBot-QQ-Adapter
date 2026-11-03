from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import base64
import pytest

from qq_official_adapter.codec import decode_message
from qq_official_adapter.plugin import MAX_INBOUND_ATTACHMENTS, QQOfficialAdapterPlugin
from qq_official_adapter.quoted_media import quoted_image_attachments


IMAGE = {"content_type": "image/png", "url": "https://grouppro.grouppro.qq.com/quoted.png"}


def payload(**changes):
    value = {
        "id": "current-message", "author": {"member_openid": "user", "user_openid": "user"},
        "group_openid": "group", "timestamp": "2026-11-03T01:00:00Z",
        "content": "这张图片是什么？", "message_type": 103,
        "msg_elements": [{"message_type": 103, "attachments": [IMAGE]}],
    }
    value.update(changes)
    return value


def plugin():
    value = QQOfficialAdapterPlugin.__new__(QQOfficialAdapterPlugin)
    value._ctx = SimpleNamespace(logger=Mock())
    value._download_attachment = AsyncMock(return_value=b"quoted-image-bytes")
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"])
async def test_quote_image_survives_attachment_conversion_and_gateway_decode(event):
    value = plugin()
    data = payload()
    segments, labels, picture, emoji = await value._build_attachments(data)
    message, _, _ = decode_message(event, data, "bot", segments, labels, picture, emoji)
    images = [part for part in message["raw_message"] if part["type"] == "image"]
    assert len(images) == 1
    assert base64.b64decode(images[0]["binary_data_base64"]) == b"quoted-image-bytes"
    assert message["is_picture"] and not message["is_emoji"]
    assert message["processed_plain_text"] == "这张图片是什么？ [引用消息中的图片]"
    assert segments[0] == {"type": "text", "data": "[以下图片来自引用消息]"}
    assert segments[-1] == {"type": "text", "data": "[引用图片结束]"}
    value._download_attachment.assert_awaited_once_with(IMAGE["url"])


@pytest.mark.asyncio
async def test_current_attachments_and_quote_share_the_existing_count_limit():
    value = plugin()
    current = [{**IMAGE, "url": IMAGE["url"] + f"?current={i}"} for i in range(MAX_INBOUND_ATTACHMENTS)]
    segments, labels, _, _ = await value._build_attachments(payload(attachments=current))
    assert len(segments) == MAX_INBOUND_ATTACHMENTS
    assert labels == ["[图片]"] * MAX_INBOUND_ATTACHMENTS
    assert value._download_attachment.await_count == MAX_INBOUND_ATTACHMENTS
    value._download_attachment.reset_mock()
    segments, _, _, _ = await value._build_attachments(payload(attachments=current[:-1]))
    assert sum(part["type"] == "image" for part in segments) == MAX_INBOUND_ATTACHMENTS
    assert value._download_attachment.await_count == MAX_INBOUND_ATTACHMENTS


@pytest.mark.asyncio
async def test_quote_image_not_classified_by_current_emoji_hint():
    _, _, picture, emoji = await plugin()._build_attachments(payload(content="[表情]"))
    assert picture and not emoji


@pytest.mark.asyncio
async def test_failed_quote_download_preserves_context_marker_without_fake_image():
    value = plugin()
    value._download_attachment.return_value = b""
    segments, labels, _, _ = await value._build_attachments(payload())
    assert not any(part["type"] == "image" for part in segments)
    assert segments[1] == {"type": "text", "data": "[图片]"}
    assert labels == ["[引用消息中的图片]"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["https://127.0.0.1/image.png", "https://example.com/image.png", "http://grouppro.grouppro.qq.com/image.png"])
async def test_quoted_image_uses_existing_url_validation_before_network(url):
    value = plugin()
    value._download_attachment = QQOfficialAdapterPlugin._download_attachment.__get__(value)
    value._require_session = Mock(side_effect=AssertionError("Unsafe URL must be rejected before HTTP"))
    segments, _, _, _ = await value._build_attachments(payload(msg_elements=[
        {"message_type": 103, "attachments": [{**IMAGE, "url": url}]},
    ]))
    assert not any(part["type"] == "image" for part in segments)
    value._require_session.assert_not_called()


@pytest.mark.asyncio
async def test_quote_only_image_with_empty_current_text_is_still_a_message():
    value = plugin()
    data = payload(content="")
    segments, labels, picture, emoji = await value._build_attachments(data)
    message, _, _ = decode_message("GROUP_MESSAGE_CREATE", data, "bot", segments, labels, picture, emoji)
    assert message["processed_plain_text"] == "[引用消息中的图片]"


def test_normal_parallel_and_forwarded_chat_elements_are_not_expanded_as_quotes():
    for kind in (0, 101, 102):
        assert not quoted_image_attachments(payload(message_type=kind, msg_elements=[
            {"message_type": kind, "attachments": [IMAGE]},
        ]), 10)
    forward = {"message_type": 102, "msg_elements": [{"message_type": 103, "attachments": [IMAGE]}]}
    assert not quoted_image_attachments(payload(msg_elements=[forward]), 10)


def test_nested_quote_images_are_deduplicated_and_bounded():
    data = payload(msg_elements=[{"message_type": "103", "attachments": [IMAGE, IMAGE],
                                "msg_elements": [{"message_type": 0, "attachments": [IMAGE]}]}])
    assert quoted_image_attachments(data, 10) == [IMAGE]
    assert quoted_image_attachments(data, 0) == []
    data["msg_elements"][0]["attachments"] = [{**IMAGE, "url": IMAGE["url"] + str(i)} for i in range(100)]
    assert len(quoted_image_attachments(data, 3)) == 3


def test_depth_and_item_budgets_and_malformed_elements():
    node = {"message_type": 103, "attachments": [IMAGE]}
    for _ in range(10):
        node = {"message_type": 103, "msg_elements": [node]}
    assert not quoted_image_attachments(payload(msg_elements=[node]), 10)
    assert not quoted_image_attachments(payload(msg_elements=[None] * 100 + [{"message_type": 103, "attachments": [IMAGE]}]), 10)
    assert not quoted_image_attachments(payload(msg_elements="bad"), 10)
    assert not quoted_image_attachments(payload(msg_elements=[{"message_type": 103, "attachments": "bad"}]), 10)
    assert not quoted_image_attachments(payload(msg_elements=[{"message_type": 103, "attachments": [None, {"content_type": "audio/wav", "url": IMAGE["url"]}]}]), 10)
