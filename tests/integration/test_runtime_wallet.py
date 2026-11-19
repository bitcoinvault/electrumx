"""Offline wallet smoke for the installed Linux candidate. From the repo root:

docker run --rm --pull=never --network none --mount "type=bind,source=${PWD}/tests/integration,target=/smoke,readonly" --entrypoint python3 btcv-electrumx:runtime-local -B /smoke/test_runtime_wallet.py

Only the Core HTTP RPC is a stub. ElectrumX indexes serialized blocks in a
fresh LevelDB and serves wallet reads over TCP, before and after restart.
This tests indexing, not Core consensus, coinbase maturity or spendability.
"""

import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer


LOOPBACK = "127.0.0.1"
GENESIS = "01cead69f2b51e214e1c2cd50e3744428cae526db87b2ff8f32489ff801d0f1d"
TIP = "0599fae4c64670e17b4e94f4f17ef6f654d3300898b6e272e5b567e5a04aca71"
TXID = "546889787a4a2e0085400483da4bd09bdd88ed905bf2257a5934d0d826fda7fd"
SCRIPTHASH = "0e86768a14a61e71306f240f5b8bb92ced2f1abfb246b82d9356549834c2f6e2"
VAULT_SCRIPTHASH = "b9bb266fbc2b439224bd9824244920ed7d8196bdc8f9600ec3f92d561aa59fa0"
VALUE = 123456789


def double_sha(data):
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def coinbase(script_sig, value, script_pubkey):
    return (bytes.fromhex("0100000001") + bytes(32) + b"\xff" * 4
            + bytes([len(script_sig)]) + script_sig + b"\xff" * 4 + b"\x01"
            + struct.pack("<Q", value) + bytes([len(script_pubkey)]) + script_pubkey
            + bytes(4))


def block(prev, tx, timestamp, nonce):
    header = struct.pack("<I32s32sIII", 1, prev, double_sha(tx), timestamp,
                         0x207fffff, nonce)
    return header + b"\x01" + tx


def regtest_chain():
    # BTCV Core src/chainparams.cpp, CreateGenesisBlock and CRegTestParams.
    message = b"The Washington Post 07/Oct/2019 In Siberia, a mammoth thaw"
    genesis = coinbase(bytes.fromhex("04ffff001d0104") + bytes([len(message)]) + message,
                       5000000000, bytes.fromhex(
                           "4104678afdb0fe5548271967f1a67130b7105cd6a828e03909a67962e0ea1f61de"
                           "b649f6bc3f4cef38c4f35504e51ec112de5c384df7ba0b8d578a4c702b6bf11d5fac"))
    first = block(bytes(32), genesis, 1296688602, 1)
    p2pkh = bytes.fromhex("76a914" + "11" * 20 + "88ac")
    funding = coinbase(b"\x01\x01", VALUE, p2pkh)
    # Regtest enables BTCV's alert section at height 1; zero alert txs.
    second = block(double_sha(first[:80]), funding, 1296688603, 7) + b"\x00"
    return {double_sha(raw[:80])[::-1].hex(): raw.hex() for raw in (first, second)}


def pending_alert_chain():
    # Indexing only: no signatures, consensus checks, or spendable keys.
    first = bytes.fromhex(regtest_chain()[GENESIS])
    redeem = (bytes.fromhex("6351675268") + b"\x21\x02" + b"\x11" * 32
              + b"\x21\x03" + b"\x22" * 32 + b"\x52\xae")
    redeem_hash = hashlib.new("ripemd160", hashlib.sha256(redeem).digest()).digest()
    funding = coinbase(b"\x01\x01", VALUE, b"\xa9\x14" + redeem_hash + b"\x87")
    second = block(double_sha(first[:80]), funding, 1296688603, 7) + b"\x00"
    script_sig = b"\x00\x01\x01\x4b" + redeem
    destination = bytes.fromhex("76a914" + "11" * 20 + "88ac")
    alert = (bytes.fromhex("0100000001") + double_sha(funding) + bytes(4)
             + bytes([len(script_sig)]) + script_sig + b"\xff" * 4 + b"\x01"
             + struct.pack("<Q", VALUE - 1) + bytes([len(destination)]) + destination + bytes(4))
    reward = coinbase(b"\x01\x02", 0, b"\x6a")
    third = block(double_sha(second[:80]), reward, 1296688604, 8) + b"\x01" + alert
    blocks = {double_sha(raw[:80])[::-1].hex(): raw.hex() for raw in (first, second, third)}
    return blocks, double_sha(alert)[::-1].hex()


class CoreRPC(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, req):
        method, params = req["method"], req.get("params", [])
        s = self.server
        error = result = None
        if method == "getblockcount" and not params:
            result = len(s.blocks) - 1
        elif (method == "getblockhash" and len(params) == 1 and isinstance(params[0], int)
              and 0 <= params[0] < len(s.blocks)):
            result = list(s.blocks)[params[0]]
        elif (method == "getblock" and len(params) == 2
              and params[1] is False and params[0] in s.blocks):
            s.fetched.append(params[0])
            result = s.blocks[params[0]]
        elif method == "getrawmempool" and not params:
            result = []
        else:
            s.unexpected.append((method, params))
            error = {"code": -32601, "message": "unexpected fixture request"}
        return {"id": req["id"], "result": result, "error": error}

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        reply = [self.reply(item) for item in req] if isinstance(req, list) else self.reply(req)
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@contextmanager
def core_stub(blocks=None):
    """Yield the running loopback Core fixture (use core.server_port)."""
    with HTTPServer((LOOPBACK, 0), CoreRPC) as core:
        core.blocks = regtest_chain() if blocks is None else blocks
        core.fetched, core.unexpected = [], []
        thread = threading.Thread(target=core.serve_forever, daemon=True)
        thread.start()
        try:
            yield core
        finally:
            core.shutdown()
            thread.join()


def wait_for_wallet(proc, port):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("ElectrumX exited before opening its wallet listener")
        try:
            return socket.create_connection((LOOPBACK, port), timeout=5)
        except ConnectionRefusedError:
            time.sleep(0.1)
    raise TimeoutError("ElectrumX did not open its wallet listener within 30s")


@unittest.skipUnless(__name__ == "__main__" or os.getenv("ELECTRUMX_WALLET_SMOKE") == "1",
                     "run explicitly inside the candidate container")
class RuntimeWallet(unittest.TestCase):
    def wallet_rpc(self, conn):
        request_id = 0

        def rpc(method, *params):
            nonlocal request_id
            request_id += 1
            request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            conn.write(json.dumps(request).encode() + b"\n")
            conn.flush()
            response = json.loads(conn.readline())
            self.assertEqual(response["id"], request_id, method)
            self.assertIsNone(response.get("error"), (method, response))
            return response["result"]

        return rpc

    def check_wallet(self, conn, header, version):
        rpc = self.wallet_rpc(conn)
        server, protocol = rpc("server.version", "runtime-smoke", version)
        self.assertRegex(server, r"^ElectrumX \S+")
        self.assertEqual(protocol, version)
        self.assertEqual(rpc("blockchain.headers.subscribe"), {"height": 1, "hex": header})
        row = {"height": 1, "tx_hash": TXID}
        balance = {"confirmed": VALUE, "unconfirmed": 0}
        if version == "2.0":
            row["tx_type"] = "NONVAULT"
            balance.update(alert_incoming=0, alert_outgoing=0)
        self.assertEqual(rpc("blockchain.scripthash.get_history", SCRIPTHASH), [row])
        self.assertEqual(rpc("blockchain.scripthash.get_balance", SCRIPTHASH), balance)

    def boot_and_check(self, db, core, checks=None):
        # The container has no published ports. Pick an unused local wallet port.
        with socket.socket() as probe:
            probe.bind((LOOPBACK, 0))
            port = probe.getsockname()[1]
        env = dict(os.environ, COIN="BitcoinVault", NET="regtest", DB_ENGINE="leveldb",
                   DB_DIRECTORY=str(db), ALLOW_ROOT="1", PEER_DISCOVERY="off", REPORT_SERVICES="",
                   DAEMON_URL=f"http://fixture:fixture@{LOOPBACK}:{core.server_port}/",
                   SERVICES=f"tcp://{LOOPBACK}:{port}", PYTHONUNBUFFERED="1")
        with tempfile.TemporaryFile() as log:
            proc = subprocess.Popen(["electrumx_server"], cwd=str(db), env=env,
                                    stdout=log, stderr=subprocess.STDOUT)
            try:
                if checks is None:
                    checks = [lambda conn, v=v: self.check_wallet(conn, core.blocks[TIP][:160], v)
                              for v in ("1.4", "2.0")]
                for check in checks:
                    with wait_for_wallet(proc, port) as sock, sock.makefile("rwb") as conn:
                        check(conn)
                self.assertIsNone(proc.poll(), "ElectrumX exited before SIGTERM")
                proc.terminate()
                self.assertEqual(proc.wait(timeout=15), 0)
                log.seek(0)
                output = log.read().decode(errors="replace")
                self.assertIn("shutdown complete", output)
                self.assertIn("ElectrumX server terminated normally", output)
                self.assertNotIn("Traceback (most recent call last)", output)
            except BaseException:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                log.seek(0)
                print(f"Core raw block fetches: {core.fetched}; unexpected: {core.unexpected}",
                      file=sys.stderr)
                print(log.read().decode(errors="replace"), file=sys.stderr)
                raise

    def test_funded_wallet(self):
        with tempfile.TemporaryDirectory() as tmp, core_stub() as core:
            self.assertEqual(list(core.blocks), [GENESIS, TIP])
            db = Path(tmp)
            self.boot_and_check(db, core)
            self.assertEqual(core.fetched, [GENESIS, TIP])
            for store in ("hist", "utxo"):
                self.assertTrue((db / store / "CURRENT").is_file(), store)
            core.fetched.clear()
            self.boot_and_check(db, core)
            self.assertEqual(core.fetched, [], "reopened DB should not refetch blocks")
            self.assertEqual(core.unexpected, [])

    def test_pending_alert_wallet_views(self):
        blocks, alert_txid = pending_alert_chain()

        def check(conn, protocol):
            rpc = self.wallet_rpc(conn)
            self.assertEqual(rpc("server.version", "vault-read-smoke", protocol)[1], protocol)
            history = rpc("blockchain.scripthash.get_history", SCRIPTHASH)
            unspent = rpc("blockchain.scripthash.listunspent", SCRIPTHASH)
            status = rpc("blockchain.scripthash.subscribe", SCRIPTHASH)
            if protocol == "2.0":
                self.assertEqual(history, [{"height": 2, "tx_hash": alert_txid,
                                            "tx_type": "ALERT_PENDING"}])
                self.assertEqual(unspent, [{"height": 2, "tx_hash": alert_txid, "tx_pos": 0,
                                            "value": VALUE - 1, "spend_tx_num": -1}])
                preimage = f"{alert_txid}:2:ALERT_PENDING:".encode()
                self.assertEqual(status, hashlib.sha256(preimage).hexdigest())
                incoming = {"confirmed": 0, "unconfirmed": 0, "alert_incoming": VALUE - 1,
                            "alert_outgoing": 0}
                outgoing = {"confirmed": 0, "unconfirmed": 0, "alert_incoming": 0,
                            "alert_outgoing": VALUE}
            else:
                self.assertEqual(history, [])
                self.assertEqual(unspent, [])
                self.assertIsNone(status)
                incoming = {"confirmed": 0, "unconfirmed": VALUE - 1}
                outgoing = {"confirmed": 0, "unconfirmed": 0}

            method = "blockchain.scripthash.get_balance"
            self.assertEqual(rpc(method, SCRIPTHASH), incoming)
            self.assertEqual(rpc(method, VAULT_SCRIPTHASH), outgoing)
            self.assertEqual(rpc(method, SCRIPTHASH + "," + VAULT_SCRIPTHASH),
                             {k: incoming[k] + outgoing[k] for k in incoming})

            batch = [{"jsonrpc": "2.0", "id": n, "method": method, "params": [h]}
                     for n, h in ((10, SCRIPTHASH), (11, VAULT_SCRIPTHASH))]
            conn.write(json.dumps(batch).encode() + b"\n")
            conn.flush()
            replies = json.loads(conn.readline())
            self.assertCountEqual([r["id"] for r in replies], [10, 11])
            self.assertEqual({r["id"]: r["result"] for r in replies}, {10: incoming, 11: outgoing})

        with tempfile.TemporaryDirectory() as tmp, core_stub(blocks) as core:
            checks = [lambda conn: check(conn, "1.4"), lambda conn: check(conn, "2.0")]
            self.boot_and_check(Path(tmp), core, checks)
            self.assertEqual(core.fetched, list(blocks))
            core.fetched.clear()
            self.boot_and_check(Path(tmp), core, checks)
            self.assertEqual(core.fetched, [])
            self.assertEqual(core.unexpected, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
