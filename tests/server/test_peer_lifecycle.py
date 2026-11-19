import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from electrumx.lib.coins import BitcoinVault
from electrumx.server.env import Env
from electrumx.server.session import SessionManager


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before-sync", "discovery-off", "discovery-on"])
async def test_admin_peer_monitor_follows_server_lifetime(monkeypatch, tmp_path, phase):
    class Coin(BitcoinVault):
        class SESSIONCLS(BitcoinVault.SESSIONCLS):
            pass

    monkeypatch.setenv("DB_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("DAEMON_URL", "unused")
    monkeypatch.setenv("SERVICES", "rpc://127.0.0.1:8000")
    monkeypatch.setenv("PEER_DISCOVERY", "off" if phase == "discovery-off" else "on")
    env = Env(Coin)
    server = SessionManager(env, SimpleNamespace(db_height=0), None, None, None, asyncio.Event())
    peers = server.peer_mgr
    rpc_ready, synced, discovery_ready = (asyncio.Event() for _ in range(3))
    started, stopped = asyncio.Event(), asyncio.Event()
    listener = Mock(wait_closed=AsyncMock())

    async def listen(services):
        for service in services:
            server.servers[service] = listener
            rpc_ready.set()

    async def monitor(peer):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    discover = peers.discover_peers

    async def discovery():
        await discover()
        discovery_ready.set()  # Discovery off returns without owning the monitors.

    monkeypatch.setattr(server, "_start_servers", listen)
    monkeypatch.setattr(peers, "_monitor_peer", monitor)
    join = AsyncMock(wraps=peers.group.join)
    monkeypatch.setattr(peers.group, "join", join)
    monkeypatch.setattr(peers, "_refresh_blacklist", AsyncMock())
    monkeypatch.setattr(peers, "_detect_proxy", AsyncMock())
    monkeypatch.setattr(peers, "_import_peers", AsyncMock(side_effect=discovery_ready.set))
    monkeypatch.setattr(peers, "discover_peers", discovery)
    serving = asyncio.create_task(server.serve(AsyncMock(), synced))
    try:
        async with asyncio.timeout(5):
            await rpc_ready.wait()
            peer = "node.example s50002"
            assert await server.rpc_add_peer(peer) == f"peer '{peer}' added"
            await started.wait()
            if phase != "before-sync":
                synced.set()
                await discovery_ready.wait()
            serving.cancel()
            with pytest.raises(asyncio.CancelledError):
                await serving
            assert stopped.is_set() and peers.group.joined
            late_peer = "later.example s50002"
            assert await server.rpc_add_peer(late_peer) == (
                f"peer '{late_peer}' not added: peer discovery is stopping")
            assert {p.host for p in peers.peers} == {"node.example"}
            await peers.shutdown()  # Already drained.
            join.assert_awaited_once()
            listener.wait_closed.assert_awaited_once()
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
