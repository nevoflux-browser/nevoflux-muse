import asyncio

import anyio
import pytest
from mcp import MCPError
from mcp.shared.message import SessionMessage
from mcp_types import CONNECTION_CLOSED, jsonrpc_message_adapter

from nevoflux_muse import session as session_mod
from nevoflux_muse.auth import AuthRevoked
from nevoflux_muse.envelope import Envelope
from nevoflux_muse.pairing import Pairing
from nevoflux_muse.session import (
    AccountMismatch,
    HeadConnection,
    IncompatibleHead,
    RelayRejected,
)
from nevoflux_muse.testing import FakeHead, FakeRelay
from nevoflux_muse.transport import RelayLink, RelayRefused

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


def patch_connect(monkeypatch, statuses):
    """Make RelayLink.connect refuse with each status in turn, then behave."""
    real = RelayLink.connect
    todo = list(statuses)

    async def connect(*args, **kwargs):
        if todo:
            raise RelayRefused(todo.pop(0))
        return await real(*args, **kwargs)

    monkeypatch.setattr(RelayLink, "connect", connect)


async def test_relay_400_is_fatal(relay, monkeypatch):
    patch_connect(monkeypatch, [400])
    async with HeadConnection(pairing(relay), fixed_token, **FAST) as conn:
        with pytest.raises(RelayRejected):
            await conn.session(timeout=5)
        assert conn.state == "relay_refused"


@pytest.mark.parametrize("status", [404, 429, 503])
async def test_other_relay_statuses_are_retried(relay, head, monkeypatch, status):
    patch_connect(monkeypatch, [status] * 4)
    async with HeadConnection(pairing(relay), fixed_token, **FAST) as conn:
        await conn.session(timeout=5)
        assert conn.state == "connected"


def count(head, method):
    return sum(1 for m in head.requests if m.get("method") == method)


async def test_initialize_timeout_redials(relay):
    head = await FakeHead(relay.url, CHANNEL, KEY, ignore_initialize=1).start()
    try:
        async with HeadConnection(pairing(relay), fixed_token, **FAST) as conn:
            s = await conn.session(timeout=5)
            await s.send_ping()
            assert count(head, "initialize") == 2
    finally:
        await head.stop()


async def test_a_session_that_ends_on_its_own_redials(relay):
    head = await FakeHead(relay.url, CHANNEL, KEY, fail_initialize=1).start()
    try:
        async with HeadConnection(pairing(relay), fixed_token, **FAST) as conn:
            s = await conn.session(timeout=5)
            await s.send_ping()
            assert count(head, "initialize") == 2
    finally:
        await head.stop()


async def test_head_and_error_properties(relay, head):
    async with HeadConnection(pairing(relay), fixed_token, **FAST) as conn:
        assert conn.head is None and conn.error is None
        await conn.session(timeout=5)
        assert conn.head == {"name": "nevoflux-head", "version": "0.0.0-fake", "protocol": 1}
    async def revoked():
        raise AuthRevoked("gone")
    async with HeadConnection(pairing(relay), revoked, **FAST) as conn:
        await until(lambda: conn.state == "auth_revoked")
        assert isinstance(conn.error, AuthRevoked) and conn.head is None


async def test_exit_lets_the_callers_cancellation_through():
    conn = HeadConnection(Pairing("ws://127.0.0.1:9", CHANNEL, KEY), fixed_token, **FAST)

    async def stubborn():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(10)  # a slow teardown

    conn._task = asyncio.create_task(stubborn())
    await asyncio.sleep(0)
    closer = asyncio.create_task(conn.__aexit__(None, None, None))
    await asyncio.sleep(0.05)
    closer.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closer, 1)
    finally:
        conn._task.cancel()
        await asyncio.wait({conn._task})


async def test_teardown_waits_for_a_cancelled_session_task(monkeypatch):
    monkeypatch.setattr(session_mod, "TEARDOWN_GRACE", 0.05)
    conn = HeadConnection(Pairing("ws://127.0.0.1:9", CHANNEL, KEY), fixed_token, **FAST)

    async def slow_to_die():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)

    task = asyncio.create_task(slow_to_die())
    await asyncio.sleep(0)
    conn._session_task = task
    await conn._drop_session()
    assert task.done()


class RecordingConnection(HeadConnection):
    async def _backoff(self, delay):
        self.delays.append(delay)
        await asyncio.sleep(0.01)


def capture_links(monkeypatch, statuses=()):
    real = RelayLink.connect
    todo = list(statuses)
    links = []

    async def connect(*args, **kwargs):
        if todo:
            raise RelayRefused(todo.pop(0))
        link = await real(*args, **kwargs)
        links.append(link)
        return link

    monkeypatch.setattr(RelayLink, "connect", connect)
    return links


async def test_backoff_resets_after_a_session_went_live(relay, head, monkeypatch):
    links = capture_links(monkeypatch, [503, 503])
    conn = RecordingConnection(pairing(relay), fixed_token, challenge_timeout=0.5,
                               backoff_initial=0.05, backoff_max=10)
    conn.delays = []
    async with conn:
        await conn.session(timeout=5)
        assert conn.delays == [0.05, 0.1]
        await links[-1].close()
        await until(lambda: len(conn.delays) == 3)
        assert conn.delays[2] == 0.05
        await conn.session(timeout=5)


async def test_a_challenge_from_an_earlier_connection_is_refused(relay, head, monkeypatch):
    links = capture_links(monkeypatch)
    async with HeadConnection(pairing(relay), fixed_token, **FAST) as conn:
        await conn.session(timeout=5)
        await links[-1].close()
        await until(lambda: len(links) == 2 and conn.state == "connected")
        s2 = await conn.session(timeout=5)
        await head.replay_challenge(-2)  # the first connection's challenge
        await s2.send_ping()
        assert await conn.session(timeout=1) is s2
        assert count(head, "initialize") == 2
