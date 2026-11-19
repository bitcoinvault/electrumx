from struct import pack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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


@pytest.mark.asyncio
@pytest.mark.parametrize('alert_count', [1, 2])
async def test_recovery_reorg_restores_each_pending_outpoint(open_index, monkeypatch, alert_count):
    bp = await open_index()
    funding = [tx(10 + i) for i in range(alert_count + 1)]
    for item in funding:
        advance(bp, item)

    alerts, outpoints, expected = [], [], []
    for i in range(alert_count):
        alert, tx_hash = tx(20 + i, VaultTxType.ALERT_PENDING, funding[i][1])
        outputs = []
        for vout, amount in enumerate((19 + i, 29 - i)):
            script = b'\x76\xa9\x14' + bytes([1 + 2 * i + vout]) * 20 + b'\x88\xac'
            outputs.append(TxOutput(amount, script))
            outpoints.append((tx_hash, vout))
            expected.append((bp.coin.hashX_from_script(script), amount))
        alerts.append((alert._replace(outputs=outputs), tx_hash))

    advance(bp, tx(30), alerts)
    await bp.flush(True)
    old_tip = bp.tip
    before = await bp.db.lookup_utxos(outpoints)
    assert before == expected
    pending = [await bp.db.all_utxos(hashX) for hashX, _ in before]
    for (tx_hash, vout), (_, amount), (utxo,) in zip(outpoints, before, pending):
        assert (utxo.tx_hash, utxo.tx_pos, utxo.value, utxo.spend_tx_num) == (
            tx_hash, vout, amount, -1)

    recoveries = [tx(40 + i, VaultTxType.RECOVERY, funding[i][1]) for i in range(alert_count)]
    # A new alert also contributes an input record before the recovered-output suffix.
    new_alert = tx(50, VaultTxType.ALERT_PENDING, funding[-1][1])
    header = bytes(4) + old_tip + bytes(44)
    block = BitcoinVaultBlock(header, header, [tx(31), *recoveries], [new_alert])
    bp.advance_blocks([block])
    await bp.flush(True)
    bp = await open_index()
    assert await bp.db.lookup_utxos(outpoints) == [None] * len(outpoints)

    # Pin the legacy bytes from disk: each alert's outputs have no vout field.
    records = [hashX + pack('<IQi', utxo.tx_num, amount, -1)
               for (hashX, amount), (utxo,) in zip(before, pending)]
    groups = [records[j] + records[j + 1] for j in range(0, len(records), 2)]
    undo = bp.db.read_undo_info(bp.height)
    suffix = undo[(len(recoveries) + 1) * len(records[0]):]
    assert suffix in (b''.join(groups), b''.join(reversed(groups)))

    # Supply the already decoded fixture block; backup, flush and reopen use LevelDB.
    decoder = Mock(return_value=block)
    monkeypatch.setattr(bp.coin, 'block', decoder)
    bp.daemon.height = AsyncMock(return_value=bp.height)
    old_height = bp.height - 1
    await bp.reorg_chain(1)
    decoder.assert_called_once_with(block.raw, old_height + 1)
    assert await bp.db.lookup_utxos(outpoints) == before
    bp = await open_index()
    assert (bp.db.db_height, bp.db.db_tip) == (old_height, old_tip)
    assert await bp.db.lookup_utxos(outpoints) == before
    assert [await bp.db.all_utxos(hashX) for hashX, _ in before] == pending
