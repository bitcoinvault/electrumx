import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiorpcx import RPCSession, SessionKind, TaskTimeout
from aiorpcx.websocket import WSTransport
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from electrumx.lib.coins import BitcoinVault


@pytest.fixture
def session():
    client = object.__new__(BitcoinVault.SESSIONCLS)
    client.logger = Mock()
    client.subscribe_headers = True
    client.hashX_subs = {b'a': 'first', b'b': 'second'}
    client.mempool_statuses = {}
    client.send_notification = AsyncMock()
    client.subscribe_headers_result = AsyncMock(return_value={'height': 12})
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize('headers', [True, False])
async def test_send_timeout_stops_this_batch(session, headers):
    session.subscription_address_status = AsyncMock(return_value='status')
    session.send_notification.side_effect = TaskTimeout(1)
    await session.notify(set(session.hashX_subs), headers)
    session.send_notification.assert_awaited_once()
    method, args = session.send_notification.await_args.args
    if headers:
        assert (method, args) == ('blockchain.headers.subscribe', ({'height': 12},))
        session.subscription_address_status.assert_not_awaited()
    else:
        assert method == 'blockchain.scripthash.subscribe'
        assert args[0] in session.hashX_subs.values() and args[1] == 'status'


@pytest.mark.asyncio
@pytest.mark.parametrize('closed', [ConnectionClosedOK, ConnectionClosedError])
async def test_websocket_close_stops_subscription_batch(session, closed):
    websocket = Mock(send=AsyncMock(side_effect=closed(None, None)))
    transport = WSTransport(websocket, RPCSession, SessionKind.SERVER)
    session.send_notification = transport.session.send_notification
    session.subscription_address_status = AsyncMock()
    await session.notify(set(session.hashX_subs), True)
    websocket.send.assert_awaited_once()
    session.subscription_address_status.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [asyncio.CancelledError(), RuntimeError('internal error')])
async def test_other_send_errors_propagate(session, error):
    session.send_notification.side_effect = error
    with pytest.raises(type(error)) as raised:
        await session.notify(set(), True)
    assert raised.value is error


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [OSError('history read failed'), TaskTimeout(3)])
async def test_history_errors_propagate_before_send(session, error):
    session.session_mgr = SimpleNamespace(limited_history=AsyncMock(side_effect=error))
    with pytest.raises(type(error)) as raised:
        await session.notify({b'a'}, False)
    assert raised.value is error
    session.send_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_header_and_scripthash_delivery(session):
    session.subscription_address_status = AsyncMock(return_value='status')
    await session.notify({b'a'}, True)
    assert [call.args for call in session.send_notification.await_args_list] == [
        ('blockchain.headers.subscribe', ({'height': 12},)),
        ('blockchain.scripthash.subscribe', ('first', 'status')),
    ]
