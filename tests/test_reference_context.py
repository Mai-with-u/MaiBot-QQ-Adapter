from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from codec import decode_message, extract_command_text
from reference_context import attach_reference_context, reference_scope
from plugins.qq_official_adapter.plugin import QQOfficialAdapterPlugin


def payload(**kwargs):
    value = dict(id='question', content='who said it?', author={'member_openid': 'bob'}, group_openid='group', timestamp='2026-10-30T10:00:00+08:00')
    value.update(kwargs)
    return value


def decode(value):
    message = decode_message('GROUP_AT_MESSAGE_CREATE', value, 'bot')[0]
    attach_reference_context(message, value)
    return message


def test_context_survives_nonempty_current_content():
    message = decode(payload(msg_elements=[{'content': 'older words'}]))
    assert 'older words' in message['processed_plain_text']
    assert not any(s['type'] == 'reply' for s in message['raw_message'])


def test_explicit_reference_and_author():
    value = payload(message_reference={'message_id': 'original'}, msg_elements=[{'id': 'original', 'author': {'username': 'Alice'}, 'content': 'older words'}])
    message = decode(value)
    assert message['raw_message'][0] == {'type': 'reply', 'data': 'original'}
    assert 'Alice' in message['processed_plain_text']


def test_only_quoted_command_is_not_a_command():
    value = payload(content='', message_scene={'ext': ['ref_msg_idx=IDX']}, msg_elements=[{'msg_idx': 'IDX', 'content': '/assign_id 123'}])
    assert extract_command_text(value) == ''
    assert decode(value)['is_command'] is False


def test_history_candidate_is_labelled_without_reply_id():
    value = payload(message_scene={'ext': ['ref_msg_idx=missing']}, msg_elements=[{'content': 'older words'}], _qq_reference_history={'status': 'history_author_candidate', 'author': {'username': 'Alice'}})
    message = decode(value)
    assert 'Alice' in message['processed_plain_text']
    assert '非平台引用索引确认' in message['processed_plain_text']
    assert not any(s['type'] == 'reply' for s in message['raw_message'])


def test_multiple_authors_have_no_name():
    message = decode(payload(message_scene={'ext': ['ref_msg_idx=missing']}, _qq_reference_history={'status': 'ambiguous_authors'}))
    assert '多个作者' in message['processed_plain_text']


def test_scope_uses_raw_account_and_conversation():
    assert reference_scope('bot-a', 'GROUP_MESSAGE_CREATE', payload()) != reference_scope('bot-b', 'GROUP_MESSAGE_CREATE', payload())


@pytest.mark.asyncio
async def test_real_receive_routes_cached_author_and_context(tmp_path):
    plugin = QQOfficialAdapterPlugin()
    plugin.set_plugin_config(plugin.get_default_config())
    plugin._set_context(SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path), logger=Mock(), gateway=SimpleNamespace(route_message=AsyncMock(return_value=True))))
    plugin._ready = True
    plugin._inbound_ready.set()
    await plugin._receive_message('GROUP_MESSAGE_CREATE', payload(id='original', content='older words', author={'member_openid': 'alice', 'username': 'Alice'}, message_scene={'ext': ['msg_idx=IDX']}), account_id='bot', bot_name='Bot')
    await plugin._receive_message('GROUP_AT_MESSAGE_CREATE', payload(message_scene={'ext': ['ref_msg_idx=IDX']}, msg_elements=[{'msg_idx': 'IDX', 'content': 'older words'}]), account_id='bot', bot_name='Bot')
    message = plugin.ctx.gateway.route_message.call_args.kwargs['message']
    assert 'Alice' in message['processed_plain_text']
    assert message['raw_message'][0] == {'type': 'reply', 'data': 'original'}


@pytest.mark.asyncio
async def test_real_receive_uses_opt_in_history_fallback(tmp_path):
    plugin = QQOfficialAdapterPlugin()
    plugin.set_plugin_config(plugin.get_default_config())
    plugin.config.advanced.reference_history_fallback = True
    plugin._set_context(SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path), logger=Mock(), gateway=SimpleNamespace(route_message=AsyncMock(return_value=True))))
    plugin._ready = True
    plugin._inbound_ready.set()
    await plugin._receive_message('GROUP_MESSAGE_CREATE', payload(id='original', content='older words', author={'member_openid': 'alice', 'username': 'Alice'}, message_scene={'ext': ['msg_idx=IDX']}), account_id='bot', bot_name='Bot')
    await plugin._receive_message('GROUP_AT_MESSAGE_CREATE', payload(message_scene={'ext': ['ref_msg_idx=OTHER']}, msg_elements=[{'msg_idx': 'OTHER', 'content': 'older words'}]), account_id='bot', bot_name='Bot')
    message = plugin.ctx.gateway.route_message.call_args.kwargs['message']
    assert 'Alice' in message['processed_plain_text']
    assert '非平台引用索引确认' in message['processed_plain_text']
    assert not any(s['type'] == 'reply' for s in message['raw_message'])

