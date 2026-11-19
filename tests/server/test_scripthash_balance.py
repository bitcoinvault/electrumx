from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from aiorpcx import RPCError

from electrumx.lib.tx import VaultTxType
from electrumx.server.mempool import BitcoinVaultMemPool
from electrumx.server.session import (
    BAD_REQUEST, BitcoinVaultElectrumX, ElectrumX, scripthash_to_hashX,
)


SOURCE, DEST = '11' * 32, '22' * 32
SRC_X, DST_X = map(scripthash_to_hashX, (SOURCE, DEST))


def vault_session(protocol):
    session = object.__new__(BitcoinVaultElectrumX)
    session.protocol_tuple = protocol
    session.bump_cost = Mock()
    # Normal outputs, plus both sides of a pending alert already in a block.
    rows = {SRC_X: [(1000, 0), (12, 3)], DST_X: [(50, 0), (20, -1)]}
    session.db = Mock(all_utxos=AsyncMock(side_effect=lambda h: [
        SimpleNamespace(value=v, spend_tx_num=n) for v, n in rows[h]]))
    # A different, unmined alert: debit 100, credit 90. Also an ordinary tx.
    pool = object.__new__(BitcoinVaultMemPool)
    pool.txs = {
        b'a': SimpleNamespace(type=VaultTxType.ALERT_PENDING, in_pairs=[(SRC_X, 100)],
                              out_pairs=[(DST_X, 90)]),
        b'b': SimpleNamespace(type=VaultTxType.NONVAULT, in_pairs=[(DST_X, 7)],
                              out_pairs=[(SRC_X, 5)]),
    }
    pool.hashXs = {h: set(pool.txs) for h in rows}
    session.mempool = pool
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize(('protocol', 'source', 'destination'), [
    ((1, 4), {'confirmed': 1000, 'unconfirmed': -95},
     {'confirmed': 50, 'unconfirmed': 103}),
    ((2, 0), {'confirmed': 1000, 'unconfirmed': -95, 'alert_incoming': 0, 'alert_outgoing': 112},
     {'confirmed': 50, 'unconfirmed': 83, 'alert_incoming': 110, 'alert_outgoing': 0}),
])
async def test_vault_balance_with_mempool(protocol, source, destination):
    session = vault_session(protocol)
    assert await session.scripthash_get_balance(SOURCE) == source
    assert await session.scripthash_get_balance(DEST) == destination
    assert await session.scripthash_get_balance(','.join((DEST, SOURCE, DEST))) == {
        field: source[field] + 2 * destination[field] for field in source}
    # Each occurrence, including duplicates, pays the original per-UTXO read cost once.
    assert session.bump_cost.call_args_list == [call(1.04)] * 5
    assert session.db.all_utxos.await_args_list == [call(h) for h in
                                                  (SRC_X, DST_X, DST_X, SRC_X, DST_X)]


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['', '00', 'zz' * 32, ',' + DEST, SOURCE + ',', None, [DEST]])
async def test_invalid_scripthash_returns_no_partial_balance(invalid):
    session = vault_session((2, 0))
    with pytest.raises(RPCError) as error:
        await session.scripthash_get_balance(invalid)
    assert error.value.code == BAD_REQUEST
    assert session.db.all_utxos.await_count == (1 if invalid == SOURCE + ',' else 0)


@pytest.mark.asyncio
async def test_standard_coin_uses_scalar_delta_and_two_keys():
    session = object.__new__(ElectrumX)
    session.db = Mock(all_utxos=AsyncMock(return_value=[SimpleNamespace(value=10)]))
    session.mempool = Mock(balance_delta=AsyncMock(return_value=-3))
    session.bump_cost = Mock()
    assert await session.scripthash_get_balance(SOURCE + ',' + DEST) == {
        'confirmed': 20, 'unconfirmed': -6}
    assert session.bump_cost.call_args_list == [call(1.02), call(1.02)]
