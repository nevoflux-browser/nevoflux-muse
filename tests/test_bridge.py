import asyncio
import os
import sys

import pytest

from nevoflux_muse import ipc
from nevoflux_muse.auth import AuthRevoked
from nevoflux_muse.bridge import Bridge, serve
from nevoflux_muse.pairing import Pairing
from nevoflux_muse.session import HeadConnection
from nevoflux_muse.testing import FakeHead, FakeRelay

KEY = bytes(range(32))
CHANNEL = "chan-1"
FAST = {"challenge_timeout": 0.5, "backoff_initial": 0.05, "backoff_max": 0.2}


@pytest.fixture
async def relay():
    r = await FakeRelay().start()
    yield r
    await r.stop()


@pytest.fixture
async def head(relay):
    h = await FakeHead(relay.url, CHANNEL, KEY).start()
    yield h
    await h.stop()


async def fixed_token():
    return "test"


async def until(cond, timeout=5.0):
    async def wait():
        while not cond():
            await asyncio.sleep(0.02)
    await asyncio.wait_for(wait(), timeout)


def factory_for(relay, channel=CHANNEL, provider=fixed_token):
    return lambda: HeadConnection(Pairing(relay.url, channel, KEY), provider, **FAST)


@pytest.fixture
async def bridge(relay, tmp_path):
    b = Bridge(tmp_path, connection_factory=factory_for(relay), connect_wait=5)
    await b.start()
    yield b
    await b.stop()


async def test_ping(tmp_path):
    b = Bridge(tmp_path, connection_factory=lambda: None)
    reply = await b.handle({"op": "ping"})
    assert reply["ok"] and reply["pid"] == os.getpid() and reply["version"]


async def test_not_ready_without_files(tmp_path):
    b = Bridge(tmp_path)  # reads tmp_path: nothing there
    await b.start()
    status = await b.handle({"op": "status"})
    assert status["state"] == "not_ready" and status["head"] is None
    reply = await b.handle({"op": "call", "tool": "x", "args": {}})
    assert reply["error"]["code"] == "not_connected"


async def test_status_connected(bridge, head):
    await until(lambda: bridge.state == "connected")
    status = await bridge.handle({"op": "status"})
    assert status["ok"] and status["state"] == "connected" and status["last_error"] is None
    assert status["head"] == {"name": "nevoflux-head", "version": "0.0.0-fake", "protocol": 1}
    assert status["pid"] == os.getpid() and status["since"]


async def test_tools_and_call(bridge, head):
    tools = await bridge.handle({"op": "tools"})
    assert [t["name"] for t in tools["tools"]] == ["browser_snapshot", "browser_navigate"]
    assert tools["tools"][0]["inputSchema"] == {"type": "object"}
    reply = await bridge.handle({"op": "call", "tool": "browser_snapshot", "args": {}})
    assert reply["ok"] and reply["result"]["content"][0]["text"] == "fake browser_snapshot"
    assert reply["result"]["isError"] is False


async def test_head_errors_are_mcp_errors(bridge, head):
    reply = await bridge.handle({"op": "call", "tool": "bash", "args": {"command": "id"}})
    assert reply["error"]["code"] == "mcp_error"
    assert reply["error"]["data"]["code"] == -32602
    assert reply["error"]["data"]["data"] == {"code": "not_allowed"}


async def test_not_connected_while_the_head_is_away(relay, tmp_path):
    b = Bridge(tmp_path, connection_factory=factory_for(relay))
    await b.start()
    try:
        reply = await b.handle({"op": "tools", "connect_wait": 0.3})
        assert reply["error"]["code"] == "not_connected"
        assert reply["error"]["state"] in ("connecting", "head_offline")
    finally:
        await b.stop()


async def test_call_timeout(bridge, head):
    reply = await bridge.handle({"op": "call", "tool": "hang", "args": {}, "timeout": 0.3})
    assert reply["error"]["code"] == "timeout"


async def test_connection_lost(bridge, head):
    call = asyncio.create_task(bridge.handle({"op": "call", "tool": "hang", "args": {}}))
    await until(lambda: any((m.get("params") or {}).get("name") == "hang" for m in head.requests))
    await head.rechallenge()
    reply = await asyncio.wait_for(call, 5)
    assert reply["error"]["code"] == "connection_lost"


async def test_queued_call_times_out(relay, head, tmp_path):
    b = Bridge(tmp_path, connection_factory=factory_for(relay), max_in_flight=2, connect_wait=5)
    await b.start()
    try:
        hangs = [asyncio.create_task(b.handle({"op": "call", "tool": "hang", "args": {}}))
                 for _ in range(2)]
        await until(lambda: sum((m.get("params") or {}).get("name") == "hang"
                                for m in head.requests) == 2)
        reply = await b.handle({"op": "call", "tool": "browser_snapshot", "args": {},
                                "timeout": 0.3})
        assert reply["error"]["code"] == "timeout"
        assert not any((m.get("params") or {}).get("name") == "browser_snapshot"
                       for m in head.requests)
        assert (await b.handle({"op": "ping"}))["ok"]
    finally:
        for t in hangs:
            t.cancel()
        await b.stop()


async def test_reload_switches_pairing(relay, head, tmp_path):
    other = await FakeHead(relay.url, "chan-2", KEY).start()
    channels = iter([CHANNEL, "chan-2"])
    b = Bridge(tmp_path, connection_factory=lambda: factory_for(relay, next(channels))(),
               connect_wait=5)
    await b.start()
    try:
        await until(lambda: b.state == "connected")
        assert (await b.handle({"op": "reload"}))["ok"]
        reply = await b.handle({"op": "call", "tool": "browser_snapshot", "args": {}})
        assert reply["ok"]
        assert any(m.get("method") == "tools/call" for m in other.requests)
    finally:
        await b.stop()
        await other.stop()


async def test_a_fatal_state_answers_at_once(relay, head, tmp_path):
    async def revoked():
        raise AuthRevoked("the sign-in is gone")
    b = Bridge(tmp_path, connection_factory=factory_for(relay, provider=revoked))
    await b.start()
    try:
        await until(lambda: b.state == "auth_revoked")
        status = await b.handle({"op": "status"})
        assert status["last_error"] == "the sign-in is gone"
        reply = await asyncio.wait_for(b.handle({"op": "tools"}), 1)
        assert reply["error"]["code"] == "not_connected"
    finally:
        await b.stop()


@pytest.mark.parametrize("msg", [
    "x", [], {}, {"op": 5},
    {"op": "call"}, {"op": "call", "tool": "x", "args": []},
    {"op": "call", "tool": "x", "args": {}, "timeout": "soon"},
    {"op": "call", "tool": "x", "args": {}, "timeout": -1},
])
async def test_bad_requests(tmp_path, msg):
    b = Bridge(tmp_path, connection_factory=lambda: None)
    assert (await b.handle(msg))["error"]["code"] == "bad_request"


async def test_unknown_op(tmp_path):
    b = Bridge(tmp_path, connection_factory=lambda: None)
    assert (await b.handle({"op": "dance"}))["error"]["code"] == "unknown_op"


@pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX")
async def test_serve_answers_and_shuts_down(tmp_path):
    b = Bridge(tmp_path, connection_factory=lambda: None)
    server = await serve(b, ipc.socket_path(tmp_path))
    try:
        reply = await asyncio.to_thread(ipc.request, tmp_path, {"op": "ping"}, 5)
        assert reply["ok"]
        bad = await asyncio.to_thread(ipc.request, tmp_path, {"no": "op"}, 5)
        assert bad["error"]["code"] == "bad_request"
        await asyncio.to_thread(ipc.request, tmp_path, {"op": "shutdown"}, 5)
        await asyncio.wait_for(b.wait_stopped(), 2)
    finally:
        server.close()
        await server.wait_closed()
