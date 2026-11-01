import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from qq_official_adapter.plugin import QQOfficialAdapterPlugin, QQAPIError
from test_ark_messages import card, message


def adapter():
    plugin = QQOfficialAdapterPlugin.__new__(QQOfficialAdapterPlugin)
    plugin._ready = True
    plugin._id_map = {'user': {}, 'group': {}}
    plugin._ref_idx = {}
    plugin._replies = {('private', 'mapped-user', 'incoming'): (time.time(), 0)}
    plugin._reply_lock = asyncio.Lock()
    plugin._ctx = SimpleNamespace(logger=Mock())
    return plugin


@pytest.mark.asyncio
@pytest.mark.parametrize('exc,state', [(QQAPIError(400, 40034126, '无ark模板权限', 'messages'), 'failed'),
    (TimeoutError('transport timeout'), 'unknown'), (QQAPIError(502, None, 'upstream failure', 'messages'), 'unknown')])
async def test_exact_receipt_exposes_rejection_without_sending_again(exc, state):
    plugin = adapter()
    plugin._api_post = AsyncMock(side_effect=exc)
    before = time.time()
    assert not (await plugin.handle_qq_official_gateway(message(card())))['success']
    receipt = await plugin.ark_delivery_status('mapped-user', card(), after=before)
    assert receipt['state'] == state and receipt['sent'] is False
    if state == 'failed': assert receipt['error_code'] == 40034126
    assert (await plugin.ark_delivery_status('other-user', card(), after=before))['state'] == 'unknown'
    assert (await plugin.ark_delivery_status('mapped-user', card(), after=time.time() + 1))['state'] == 'unknown'
    plugin._api_post.assert_awaited_once()


@pytest.mark.asyncio
async def test_success_receipt_and_mapping():
    plugin = adapter()
    plugin._id_map = {'user': {'openid': 'mapped-user'}, 'group': {}}
    plugin._api_post = AsyncMock(return_value={'id': 'sent-card'})
    await plugin.handle_qq_official_gateway(message(card()))
    result = await plugin.ark_delivery_status('openid', card())
    assert result['sent'] and result['message_id'] == 'sent-card'


@pytest.mark.asyncio
async def test_explicit_private_ark_mode_keeps_text_and_reply_allowance_unchanged():
    from qq_official_adapter.settings import AdapterSettings
    plugin = adapter()
    plugin._plugin_config_instance = AdapterSettings(qq_official={'ark_private_use_passive_reply': False})
    plugin._api_post = AsyncMock(return_value={'id': 'sent-card'})
    assert (await plugin.handle_qq_official_gateway(message(card())))['success']
    body = plugin._api_post.call_args.args[1]
    assert 'msg_id' not in body and 'msg_seq' not in body
    assert plugin._replies[('private', 'mapped-user', 'incoming')][1] == 0
    assert (await plugin.ark_delivery_status('mapped-user', card()))['passive_reply'] is False
    text = message(card());text['raw_message'] = [{'type': 'text', 'data': 'hello'}]
    await plugin.handle_qq_official_gateway(text)
    assert plugin._api_post.call_args.args[1]['msg_id'] == 'incoming'
