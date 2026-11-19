"""Offline acceptance against the installed ElectrumX, independent of the wallet test.

From the candidate checkout, with IMAGE_ID set to a local Docker image ID:
docker run --rm --pull=never --network none --mount "type=bind,source=${PWD}/tests/integration,target=/smoke,readonly" --entrypoint python3 ${IMAGE_ID} -B /smoke/test_runtime_transports.py
"""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import unittest

if __package__:
    from . import test_runtime_wallet as fixture
else:
    import test_runtime_wallet as fixture


HOST = fixture.LOOPBACK
TLS_NAME = "wallet.test"
TIMEOUT = 5


def create_test_pki(directory):
    def openssl(*args):
        subprocess.run(["openssl", *args], cwd=directory, check=True, timeout=15,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    openssl("req", "-x509", "-newkey", "rsa:2048", "-noenc", "-days", "1",
            "-subj", "/CN=Offline Transport Test CA",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign",
            "-keyout", "ca.key", "-out", "ca.pem")
    openssl("req", "-new", "-newkey", "rsa:2048", "-noenc",
            "-subj", f"/CN={TLS_NAME}", "-keyout", "server.key", "-out", "server.csr")
    (directory / "leaf.ext").write_text(
        f"subjectAltName=DNS:{TLS_NAME}\nbasicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n",
        encoding="ascii")
    openssl("x509", "-req", "-days", "1", "-in", "server.csr", "-CA", "ca.pem",
            "-CAkey", "ca.key", "-set_serial", "2", "-extfile", "leaf.ext", "-out", "server.pem")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.hostname_checks_common_name = False
    context.load_verify_locations(cafile=str(directory / "ca.pem"))
    return context


def listener_ports():
    # Reserve all ports at once so the kernel cannot assign the same one twice.
    with ExitStack() as stack:
        ports = {}
        for name in ("ssl", "ws", "wss", "rpc"):
            sock = stack.enter_context(socket.socket())
            sock.bind((HOST, 0))
            ports[name] = sock.getsockname()[1]
        return ports


def line_exchange(stream):
    def exchange(message):
        stream.write(message.encode() + b"\n")
        stream.flush()
        line = stream.readline(65536)
        if not line.endswith(b"\n"):
            raise AssertionError("expected a newline-delimited Electrum response")
        return line
    return exchange


@unittest.skipUnless(__name__ == "__main__" or os.getenv("ELECTRUMX_TRANSPORT_SMOKE") == "1",
                     "run explicitly inside the candidate container")
class RuntimeTransports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stack = ExitStack()
        cls.addClassCleanup(stack.close)
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        cls.tls = create_test_pki(tmp)
        cls.core = stack.enter_context(fixture.core_stub())
        db = tmp / "db"
        db.mkdir()
        cls.ports = listener_ports()
        services = ",".join(f"{name}://{HOST}:{port}" for name, port in cls.ports.items())
        env = dict(os.environ, COIN="BitcoinVault", NET="regtest", DB_ENGINE="leveldb",
                   DB_DIRECTORY=str(db), ALLOW_ROOT="1", PEER_DISCOVERY="off", REPORT_SERVICES="",
                   DAEMON_URL=f"http://fixture:fixture@{HOST}:{cls.core.server_port}/",
                   SSL_CERTFILE=str(tmp / "server.pem"), SSL_KEYFILE=str(tmp / "server.key"),
                   SERVICES=services, PYTHONUNBUFFERED="1")
        log = stack.enter_context(tempfile.TemporaryFile())
        proc = subprocess.Popen(["electrumx_server"], cwd=db, env=env,
                                stdout=log, stderr=subprocess.STDOUT)

        def stop():
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                raise
            log.seek(0)
            output = log.read().decode(errors="replace")
            if proc.returncode != 0 or "Traceback (most recent call last)" in output:
                raise AssertionError(f"ElectrumX exited {proc.returncode}:\n{output}")
        stack.callback(stop)
        cls.pid = proc.pid
        # Wallet listeners open after initial sync; admin RPC alone can open earlier.
        with fixture.wait_for_wallet(proc, cls.ports["ssl"]) as raw:
            with cls.tls.wrap_socket(raw, server_hostname=TLS_NAME):
                pass

    def rpc(self, exchange, request_id, method, *params):
        message = json.dumps(dict(jsonrpc="2.0", id=request_id, method=method, params=params))
        response = json.loads(exchange(message))
        self.assertEqual(response["id"], request_id, method)
        self.assertIsNone(response.get("error"), (method, response))
        return response["result"]

    def check_wallet(self, exchange):
        server, protocol = self.rpc(exchange, 1, "server.version", "transport-smoke", "2.0")
        self.assertRegex(server, r"^ElectrumX \S+")
        self.assertEqual(protocol, "2.0")
        self.assertEqual(self.rpc(exchange, 2, "blockchain.headers.subscribe"),
                         {"height": 1, "hex": self.core.blocks[fixture.TIP][:160]})
        self.assertEqual(self.rpc(exchange, 3, "blockchain.scripthash.get_history", fixture.SCRIPTHASH),
                         [{"height": 1, "tx_hash": fixture.TXID, "tx_type": "NONVAULT"}])
        self.assertEqual(self.rpc(exchange, 4, "blockchain.scripthash.get_balance", fixture.SCRIPTHASH),
                         {"confirmed": fixture.VALUE, "unconfirmed": 0,
                          "alert_incoming": 0, "alert_outgoing": 0})

    def test_tls(self):
        with socket.create_connection((HOST, self.ports["ssl"]), TIMEOUT) as raw:
            with self.tls.wrap_socket(raw, server_hostname=TLS_NAME) as sock:
                with sock.makefile("rwb") as stream:
                    self.check_wallet(line_exchange(stream))

    def websocket_wallet(self, scheme):
        from websockets.sync.client import connect

        port = self.ports[scheme]
        with socket.create_connection((HOST, port), TIMEOUT) as raw:
            with connect(f"{scheme}://{TLS_NAME}:{port}/", sock=raw, proxy=None,
                         ssl=self.tls if scheme == "wss" else None,
                         open_timeout=TIMEOUT, close_timeout=TIMEOUT) as ws:
                def exchange(message):
                    ws.send(message)
                    reply = ws.recv(timeout=TIMEOUT)
                    self.assertIsInstance(reply, str)
                    self.assertFalse(reply.endswith("\n"), "WS messages are already framed")
                    return reply
                self.check_wallet(exchange)

    def test_ws(self):
        self.websocket_wallet("ws")

    def test_wss(self):
        self.websocket_wallet("wss")

    def test_local_admin_cli_getinfo(self):
        result = subprocess.run(
            ["/source/electrumx_rpc", "getinfo"],
            env=dict(os.environ, RPC_PORT=str(self.ports["rpc"])),
            capture_output=True, text=True, timeout=TIMEOUT)
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        info = json.loads(result.stdout)
        self.assertEqual(info["pid"], self.pid)
        self.assertEqual(info["coin"], "BitcoinVaultRegTest")
        self.assertEqual((info["db height"], info["daemon height"]), (1, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
