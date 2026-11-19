from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from electrumx.lib.coins import BitcoinVaultBlock, BitcoinVaultRegTest
from electrumx.lib.tx import TxInput, TxOutput, TxVault, VaultTxType
from electrumx.server.block_processor import BitcoinVaultBlockProcessor
from electrumx.server.db import BitcoinVaultDB


def tx(tag, kind=VaultTxType.NONVAULT, prev=None):
    prev_hash, prev_idx = (bytes(32), 0xffffffff) if prev is None else (prev, 0)
    txin = TxInput(prev_hash, prev_idx, b'', 0)
    return TxVault(1, [txin], [TxOutput(50, b'\x51')], 0, kind), bytes([tag]) * 32


def advance(bp, normal, alerts=()):
    header = bytes([bp.height + 2]) * 80
    bp.advance_blocks([BitcoinVaultBlock(header, header, [normal], list(alerts))])


@pytest.fixture
def open_index(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = SimpleNamespace(coin=BitcoinVaultRegTest, db_engine='leveldb',
                          db_dir=str(tmp_path), reorg_limit=10, cache_MB=1)
    db = None

    def close():
        if db is not None:
            db.history.close_db()
            db.utxo_db.close()

    async def open_processor():
        nonlocal db
        close()
        db = BitcoinVaultDB(env)
        daemon = Mock()
        daemon.cached_height.return_value = 5
        bp = BitcoinVaultBlockProcessor(env, db, daemon, Mock())
        await bp._first_open_dbs()
        return bp

    yield open_processor
    close()


@pytest.mark.asyncio
@pytest.mark.parametrize('flush, target', [
    ('none', 0), ('none', 1), ('partial', 0), ('partial', 1), ('full', 0),
])
async def test_recovery_across_reopened_index_and_adjacent_blocks(open_index, flush, target):
    bp = await open_index()
    funding = [tx(1), tx(2)]
    for item in funding:
        advance(bp, item)
    assert bp._recovery_tx_hash(0) == funding[0][1]
    await bp.flush(True)
    bp = await open_index()
    assert bp.db.db_height == bp.db.fs_height == bp.height == 1

    alerts = [tx(4, VaultTxType.ALERT_PENDING, funding[0][1]),
              tx(6, VaultTxType.ALERT_PENDING, funding[1][1])]
    ordinary = [tx(3), tx(5)]
    advance(bp, ordinary[0], [alerts[0]])
    if flush != 'none':
        await bp.flush(flush == 'full')
    advance(bp, ordinary[1], [alerts[1]])

    assert bp.db.fs_height == (1 if flush == 'none' else 2)
    assert bp.db.db_height == (2 if flush == 'full' else 1)
    for height, pair in enumerate(zip(ordinary, alerts), start=2):
        for ordinal, (_, tx_hash) in enumerate(pair):
            num = 2 * (height - 1) + ordinal
            assert bp._recovery_tx_hash(num) == tx_hash
            public_hash = tx_hash if height <= bp.db.db_height else None
            assert bp.db.fs_tx_hash(num) == (public_hash, height)

    expected = alerts[target][1]
    recovery = tx(7, VaultTxType.RECOVERY, funding[target][1])
    advance(bp, recovery)
    assert bp.tx_hashes[-1] == recovery[1] + expected
    await bp.flush(True)
    assert bp.db.fs_tx_hash(7) == (expected, 4)
    bp.backup_txs_and_atx([recovery], [])
    assert expected + bytes(2) in bp.utxo_cache


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_hashes, bad_types', [
    (b'', False), (bytes(31), False), (bytes(64), False), (bytes(96), False),
    (None, True),
], ids=['missing', 'misaligned', 'one-extra', 'two-extra', 'missing-type'])
async def test_flush_validates_before_consuming_buffers(open_index, bad_hashes, bad_types):
    bp = await open_index()
    advance(bp, tx(1))
    await bp.flush(True)
    item = tx(2)
    advance(bp, item)
    async with bp.state_lock:
        data = bp.flush_data()
        hashes, types = data.block_tx_hashes[:], data.block_tx_types[:]
        if bad_hashes is not None:
            data.block_tx_hashes[0] = bad_hashes
        if bad_types:
            data.block_tx_types[0] = b''
        buffers = (data.headers, data.block_tx_hashes, data.block_tx_types)
        before = [buffer[:] for buffer in buffers]
        with pytest.raises(BitcoinVaultDB.DBError, match='expected 1 transaction'):
            bp.db.flush_fs(data)
        assert list(buffers) == before
        assert bp.db.fs_height == bp.db.db_height == 0
        assert bp.db.hashes_file.read(32, 32) == b''
        data.block_tx_hashes[:] = hashes
        data.block_tx_types[:] = types
    await bp.flush(True)
    assert bp.db.fs_tx_hash(1) == (item[1], 1)
