import asyncio

import anyio
import pytest
from mcp import MCPError
from mcp.shared.message import SessionMessage
from mcp_types import CONNECTION_CLOSED, jsonrpc_message_adapter

from nevoflux_muse.auth import AuthRevoked
from nevoflux_muse.envelope import Envelope
from nevoflux_muse.pairing import Pairing
from nevoflux_muse.session import AccountMismatch, HeadConnection, IncompatibleHead
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


def pairing(relay):
    return Pairing(relay.url, CHANNEL, KEY)


async def fixed_token():
    return "test"


async def until(cond, timeout=5.0):
    async def wait():
        while not cond():
            await asyncio.sleep(0.02)
    await asyncio.wait_for(wait(), timeout)


async def test_round_trip(relay, head):
    async with HeadConnection(pairing(relay), fixed_token, **FAST) as conn:
        s = await conn.session(timeout=5)
        assert conn.state == "connected"
        assert [t.name for t in (await s.list_tools()).tools] == ["browser_snapshot", "browser_navigate"]
        result = await s.call_tool("browser_snapshot", {})
        assert result.content[0].text == "fake browser_snapshot"
        with pytest.raises(MCPError) as e:
            await s.call_tool("bash", {"command": "id"})
        assert e.value.code == -32602 and e.value.data == {"code": "not_allowed"}
    init = next(m for m in head.requests if m.get("method") == "initialize")
    assert init["params"]["clientInfo"]["name"] == "nevoflux-muse"


async def test_head_arrives_later(relay):
    async with HeadConnection(pairing(relay), fixed_token, **FAST) as conn:
        await until(lambda: conn.state == "head_offline")
        await asyncio.sleep(0.8)  # longer than challenge_timeout: no redial while alone
        assert conn.state == "head_offline"
        head = await FakeHead(relay.url, CHANNEL, KEY).start()
        try:
            s = await conn.session(timeout=5)
            await s.send_ping()
        finally:
            await head.stop()
        await until(lambda: conn.state == "head_offline")


async def test_new_challenge_mid_session(relay, head):
    async with HeadConnection(pairing(relay), fixed_token, **FAST) as conn:
        s1 = await conn.session(timeout=5)
        call = asyncio.create_task(s1.call_tool("hang", {}))
        await until(lambda: any((m.get("params") or {}).get("name") == "hang" for m in head.requests))
        await head.rechallenge()
        with pytest.raises(MCPError) as e:
            await asyncio.wait_for(call, 5)
        assert e.value.code == CONNECTION_CLOSED
        s2 = await conn.session(timeout=5)
        assert s2 is not s1
        assert len((await s2.list_tools()).tools) == 2


async def test_replayed_challenge_is_ignored(relay, head):
    async with HeadConnection(pairing(relay), fixed_token, **FAST) as conn:
        s1 = await conn.session(timeout=5)
        await head.replay_challenge()
        await s1.send_ping()  # still the same, working session
        assert await conn.session(timeout=1) is s1


async def test_garbage_is_dropped(relay, head):
    async with HeadConnection(pairing(relay), fixed_token, **FAST) as conn:
        s = await conn.session(timeout=5)
        for junk in [b"\x00" * 40, "text noise", b"short"]:
            await head.send_raw(junk)
        await s.send_ping()
        assert conn.state == "connected"


async def test_redials_when_no_challenge_comes(relay):
    head = await FakeHead(relay.url, CHANNEL, KEY, silent=True).start()
    try:
        async with HeadConnection(pairing(relay), fixed_token, **FAST):
            await until(lambda: head.arrivals >= 2, timeout=5)
    finally:
        await head.stop()


async def test_401_gets_a_fresh_token():
    relay = await FakeRelay(expect_token="good").start()
    head = await FakeHead(relay.url, CHANNEL, KEY, token="good").start()
    tokens = iter(["bad", "good"])
    calls = []

    async def provider():
        calls.append(1)
        return next(tokens)

    try:
        async with HeadConnection(pairing(relay), provider, **FAST) as conn:
            await conn.session(timeout=5)
        assert len(calls) == 2
    finally:
        await head.stop()
        await relay.stop()


async def test_three_401s_are_fatal():
    relay = await FakeRelay(expect_token="good").start()

    async def provider():
        return "bad"

    try:
        async with HeadConnection(pairing(relay), provider, **FAST) as conn:
            with pytest.raises(AuthRevoked):
                await conn.session(timeout=5)
            assert conn.state == "auth_revoked"
    finally:
        await relay.stop()


async def test_revoked_account_is_fatal(relay, head):
    async def provider():
        raise AuthRevoked("gone")

    async with HeadConnection(pairing(relay), provider, **FAST) as conn:
        with pytest.raises(AuthRevoked):
            await conn.session(timeout=5)
        assert conn.state == "auth_revoked"


async def test_another_account_is_fatal(relay, head):
    async def provider():
        return "someone-else"

    async with HeadConnection(pairing(relay), provider, **FAST) as conn:
        with pytest.raises(AccountMismatch):
            await conn.session(timeout=5)
        assert conn.state == "account_mismatch"


async def test_incompatible_head_is_fatal(relay):
    head = await FakeHead(relay.url, CHANNEL, KEY, protocol=2).start()
    try:
        async with HeadConnection(pairing(relay), fixed_token, **FAST) as conn:
            with pytest.raises(IncompatibleHead) as e:
                await conn.session(timeout=5)
            assert e.value.version == 2 and conn.state == "incompatible"
    finally:
        await head.stop()


async def test_wrong_key_never_connects(relay, head):
    async with HeadConnection(Pairing(relay.url, CHANNEL, bytes(32)), fixed_token, **FAST) as conn:
        with pytest.raises(asyncio.TimeoutError):
            await conn.session(timeout=1.5)
        assert conn.state in ("connecting", "head_offline")


async def test_a_superseded_session_sends_nothing_under_the_new_challenge(relay, head):
    async with HeadConnection(pairing(relay), fixed_token, **FAST) as conn:
        s1 = await conn.session(timeout=5)
        call = asyncio.create_task(s1.call_tool("hang", {}))
        await until(lambda: any((m.get("params") or {}).get("name") == "hang" for m in head.requests))
        mark = len(head.requests)
        await head.rechallenge()
        with pytest.raises(MCPError):
            await asyncio.wait_for(call, 5)
        await conn.session(timeout=5)
        assert head.requests[mark]["method"] == "initialize"


async def test_pump_drops_messages_once_its_challenge_is_superseded():
    sent = []

    class Link:
        async def send(self, obj):
            sent.append(obj)

        async def close(self):
            pass

    def challenge(ch):
        return {"d": "h2c", "n": 0, "ch": ch, "m": None}

    env = Envelope(set())
    env.inbound(challenge("a"))
    conn = HeadConnection(Pairing("ws://x", CHANNEL, KEY), fixed_token)
    read_w, read_r = anyio.create_memory_object_stream(8)
    write_w, write_r = anyio.create_memory_object_stream(8)
    task = asyncio.create_task(conn._session_main(Link(), env, "a", read_r, write_w, write_r,
                                                  asyncio.Event()))
    await until(lambda: len(sent) == 1)  # the session's initialize, sealed under "a"
    assert sent[0]["frame"]["ch"] == "a"
    env.inbound(challenge("b"))
    note = jsonrpc_message_adapter.validate_python(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}, by_name=False)
    await write_w.send(SessionMessage(note))
    await asyncio.sleep(0.2)
    assert len(sent) == 1
    await read_w.aclose()
    await asyncio.wait_for(task, 5)


async def test_a_failing_token_provider_is_fatal(relay):
    async def provider():
        raise ValueError("bad state file")

    async with HeadConnection(pairing(relay), provider, **FAST) as conn:
        with pytest.raises(ValueError):
            await conn.session(timeout=5)
        assert conn.state == "error"
