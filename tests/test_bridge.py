import asyncio
import os
import sys
from types import SimpleNamespace

import pytest
from mcp import types

from nevoflux_muse import ipc
from nevoflux_muse.approvals import APPROVAL_SCHEMA, Approval
from nevoflux_muse.auth import AuthRevoked
from nevoflux_muse.bridge import Bridge, serve
from nevoflux_muse.pairing import Pairing
from nevoflux_muse.session import HeadConnection
from nevoflux_muse.testing import FakeHead, FakeRelay
from nevoflux_muse.testing.fake_head import ask_message

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


ASKS = ("browser_snapshot", "browser_click", "browser_fill")


@pytest.fixture
async def asking_head(relay):
    h = await FakeHead(relay.url, CHANNEL, KEY, tools=ASKS,
                       ask=("browser_click", "browser_fill")).start()
    yield h
    await h.stop()


def click(**extra):
    return {"op": "call", "tool": "browser_click", "args": {"id": 3}, **extra}


def head_error_codes(head):
    out = []
    for m in head.sent:
        structured = (m.get("result") or {}).get("structuredContent") or {}
        if "error" in structured:
            out.append(structured["error"]["code"])
    return out


def running_calls():
    return [t for t in asyncio.all_tasks() if t.get_coro().__name__ == "_run_call"]


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


@pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX")
async def test_shutdown_stops_even_when_the_reply_cannot_be_written(tmp_path, monkeypatch):
    async def broken(writer, reply):
        raise ConnectionResetError

    b = Bridge(tmp_path, connection_factory=lambda: None)
    server = await serve(b, ipc.socket_path(tmp_path))
    monkeypatch.setattr(ipc, "write_reply", broken)
    try:
        with pytest.raises(ipc.BridgeDown):
            await asyncio.to_thread(ipc.request, tmp_path, {"op": "shutdown"}, 5)
        await asyncio.wait_for(b.wait_stopped(), 2)
    finally:
        server.close()
        await server.wait_closed()


class _Boom:
    async def call_tool(self, tool, args, **kw):
        raise RuntimeError("secret result text")


class _StubConn:
    state = "connected"
    head = None
    error = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def session(self, timeout):
        return _Boom()


async def test_unexpected_errors_are_internal_and_do_not_leak(tmp_path, caplog):
    b = Bridge(tmp_path, connection_factory=_StubConn)
    await b.start()
    with caplog.at_level("INFO"):
        reply = await b.handle({"op": "call", "tool": "browser_snapshot", "args": {}})
    assert reply == {"ok": False, "error": {"code": "internal",
                                            "message": "unexpected RuntimeError"}}
    assert "-> internal" in caplog.text and "secret result text" not in caplog.text
    assert "call 'browser_snapshot'" in caplog.text


async def test_reload_survives_a_factory_that_raises(tmp_path):
    calls = []

    def factory():
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("secret path")
        return _StubConn()

    b = Bridge(tmp_path, connection_factory=factory)
    await b.start()
    assert (await b.handle({"op": "reload"}))["ok"]
    status = await b.handle({"op": "status"})
    assert status["state"] == "not_ready" and status["last_error"] == "unexpected RuntimeError"


async def test_concurrent_reloads_leave_one_connection(relay, head, tmp_path):
    made = []

    def factory():
        conn = factory_for(relay)()
        made.append(conn)
        return conn

    b = Bridge(tmp_path, connection_factory=factory, connect_wait=5)
    await b.start()
    try:
        await asyncio.gather(b.reload(), b.reload())
        assert len(made) == 3
        assert all(c._task.done() for c in made[:-1])
        assert not made[-1]._task.done() and b._conn is made[-1]
    finally:
        await b.stop()


async def test_calls_carry_their_id_in_meta(bridge, head):
    assert (await bridge.handle({"op": "call", "tool": "browser_snapshot", "args": {}}))["ok"]
    sent = next(m for m in head.requests if m.get("method") == "tools/call")
    assert len(sent["params"]["_meta"]["io.nevoflux/call"]) == 8
    assert (await bridge.handle({"op": "pending"})) == {"ok": True, "pending": []}


async def test_a_call_that_needs_approval_returns_at_once(bridge, asking_head):
    reply = await asyncio.wait_for(bridge.handle(click()), 5)
    a = reply["approval_required"]
    assert reply["ok"] and set(a) == {"id", "message", "expires_in"}
    assert a["message"] == ask_message("browser_click") and 0 < a["expires_in"] <= 55
    [listed] = (await bridge.handle({"op": "pending"}))["pending"]
    assert listed["id"] == a["id"] and listed["tool"] == "browser_click"
    assert listed["message"] == ask_message("browser_click")


async def test_approve_returns_the_call_result(bridge, asking_head):
    a = (await bridge.handle(click()))["approval_required"]
    reply = await asyncio.wait_for(bridge.handle({"op": "approve", "id": a["id"]}), 5)
    assert reply["ok"] and reply["result"]["content"][0]["text"] == "fake browser_click"
    assert asking_head.answers[-1]["result"]["action"] == "accept"
    assert asking_head.answers[-1]["result"]["content"] == {"approve": True}
    assert (await bridge.handle({"op": "pending"}))["pending"] == []


async def test_deny_returns_the_heads_refusal(bridge, asking_head):
    a = (await bridge.handle(click()))["approval_required"]
    reply = await asyncio.wait_for(bridge.handle({"op": "deny", "id": a["id"]}), 5)
    assert reply["ok"] and reply["result"]["isError"] is True
    assert reply["result"]["structuredContent"]["error"]["code"] == "approval_denied"
    assert asking_head.answers[-1]["result"]["action"] == "decline"


async def test_answering_twice_or_an_unknown_id_is_expired(bridge, asking_head):
    a = (await bridge.handle(click()))["approval_required"]
    assert (await bridge.handle({"op": "approve", "id": a["id"]}))["ok"]
    again = await bridge.handle({"op": "approve", "id": a["id"]})
    assert again["error"]["code"] == "approval_expired"
    unknown = await bridge.handle({"op": "deny", "id": "0000ffff"})
    assert unknown["error"]["code"] == "approval_expired"


@pytest.mark.parametrize("msg", [
    {"op": "approve"}, {"op": "deny", "id": 5}, {"op": "approve", "id": ""},
    {"op": "approve", "id": "x", "timeout": 0},
])
async def test_bad_answers(tmp_path, msg):
    b = Bridge(tmp_path, connection_factory=lambda: None)
    assert (await b.handle(msg))["error"]["code"] == "bad_request"


async def test_approve_after_the_window_is_expired(relay, tmp_path):
    head = await FakeHead(relay.url, CHANNEL, KEY, tools=ASKS, ask=("browser_click",),
                          approval_timeout=0.8).start()
    b = Bridge(tmp_path, connection_factory=factory_for(relay), connect_wait=5,
               approval_window=0.2, approval_hold=5)
    await b.start()
    try:
        a = (await b.handle(click()))["approval_required"]
        await asyncio.sleep(0.3)
        assert (await b.handle({"op": "pending"}))["pending"] == []
        late = await b.handle({"op": "approve", "id": a["id"]})
        assert late["error"]["code"] == "approval_expired"
        await until(lambda: "approval_timeout" in head_error_codes(head))
        assert head.answers == []  # the head never saw an answer
    finally:
        await b.stop()
        await head.stop()


async def test_approval_wait_does_not_eat_the_call_timeout(relay, tmp_path):
    head = await FakeHead(relay.url, CHANNEL, KEY, tools=ASKS, ask=("browser_click",),
                          approval_timeout=8).start()
    b = Bridge(tmp_path, connection_factory=factory_for(relay), connect_wait=5,
               approval_window=5, approval_hold=6)
    await b.start()
    try:
        a = (await b.handle(click(timeout=1.0)))["approval_required"]
        await asyncio.sleep(1.5)  # longer than the call's own timeout
        reply = await asyncio.wait_for(b.handle({"op": "approve", "id": a["id"]}), 5)
        assert reply["ok"] and reply["result"]["content"][0]["text"] == "fake browser_click"
    finally:
        await b.stop()
        await head.stop()


async def test_a_lost_session_expires_open_approvals(bridge, asking_head):
    a = (await bridge.handle(click()))["approval_required"]
    await asking_head.rechallenge()
    await until(lambda: bridge.approvals.pending() == [])
    gone = await bridge.handle({"op": "approve", "id": a["id"]})
    assert gone["error"]["code"] == "approval_expired"


async def test_a_second_call_gets_its_own_approval(bridge, asking_head):
    first = (await bridge.handle(click()))["approval_required"]
    second = (await bridge.handle({"op": "call", "tool": "browser_fill",
                                   "args": {"id": 4, "value": "x"}}))["approval_required"]
    assert first["id"] != second["id"] and second["message"] == ask_message("browser_fill")
    tools = {p["id"]: p["tool"] for p in (await bridge.handle({"op": "pending"}))["pending"]}
    assert tools == {first["id"]: "browser_click", second["id"]: "browser_fill"}
    reply = await asyncio.wait_for(bridge.handle({"op": "approve", "id": second["id"]}), 5)
    assert reply["result"]["content"][0]["text"] == "fake browser_fill"


async def test_the_echoed_call_id_pins_the_approval(relay, tmp_path):
    head = await FakeHead(relay.url, CHANNEL, KEY, tools=ASKS, ask=("browser_click",),
                          echo_call_meta=True).start()
    b = Bridge(tmp_path, connection_factory=factory_for(relay), connect_wait=5)
    await b.start()
    hang = asyncio.create_task(b.handle({"op": "call", "tool": "hang", "args": {}}))
    try:
        await until(lambda: any((m.get("params") or {}).get("name") == "hang"
                                for m in head.requests))
        a = (await b.handle(click()))["approval_required"]
        assert not hang.done()
        assert [p["tool"] for p in (await b.handle({"op": "pending"}))["pending"]] == [
            "browser_click"]
        assert (await b.handle({"op": "deny", "id": a["id"]}))["ok"]
    finally:
        hang.cancel()
        await b.stop()
        await head.stop()


async def test_without_the_echo_the_oldest_running_call_gets_the_question(relay, tmp_path):
    # Documents the fallback (M2a-C D1): exact only while one call runs at a time.
    head = await FakeHead(relay.url, CHANNEL, KEY, tools=ASKS, ask=("browser_click",),
                          echo_call_meta=False).start()
    b = Bridge(tmp_path, connection_factory=factory_for(relay), connect_wait=5)
    await b.start()
    hang = asyncio.create_task(b.handle({"op": "call", "tool": "hang", "args": {}}))
    clicking = None
    try:
        await until(lambda: any((m.get("params") or {}).get("name") == "hang"
                                for m in head.requests))
        clicking = asyncio.create_task(b.handle(click()))
        reply = await asyncio.wait_for(hang, 5)
        assert reply["approval_required"]["message"] == ask_message("browser_click")
        assert not clicking.done()
    finally:
        hang.cancel()
        if clicking is not None:
            clicking.cancel()
        await b.stop()
        await head.stop()


async def test_the_log_never_holds_the_approval_message(bridge, asking_head, caplog):
    with caplog.at_level("INFO"):
        a = (await bridge.handle(click()))["approval_required"]
        assert (await bridge.handle({"op": "approve", "id": a["id"]}))["ok"]
    assert ask_message("browser_click") not in caplog.text
    assert "Run browser_click" not in caplog.text and "test asks" not in caplog.text
    assert f"approval {a['id']} asked for call 'browser_click'" in caplog.text
    assert f"approval {a['id']} -> approved" in caplog.text


async def test_an_old_head_still_answers_plain_calls(bridge, head):
    reply = await bridge.handle({"op": "call", "tool": "browser_snapshot", "args": {}})
    assert reply["ok"] and reply["result"]["isError"] is False
    init = next(m for m in head.requests if m.get("method") == "initialize")
    assert "form" in init["params"]["capabilities"]["elicitation"]


async def test_stop_and_reload_leave_no_call_running(bridge, asking_head):
    # F7: a reload takes down every call it started, approval-waiting or not.
    a = (await bridge.handle(click()))["approval_required"]
    hang = asyncio.create_task(bridge.handle({"op": "call", "tool": "hang", "args": {}}))
    await until(lambda: len(running_calls()) == 2)
    assert (await bridge.handle({"op": "reload"}))["ok"]
    assert running_calls() == []
    assert (await asyncio.wait_for(hang, 5))["error"]["code"] == "connection_lost"
    assert bridge.approvals.pending() == []
    gone = await bridge.handle({"op": "approve", "id": a["id"]})
    assert gone["error"]["code"] == "approval_expired"


class _TwoQuestions:
    """A head stand-in whose call asks twice, `gap` seconds after each answer."""

    def __init__(self, bridge, gap):
        self.bridge, self.gap = bridge, gap

    async def call_tool(self, tool, args, *, meta, **kw):
        ask = types.ElicitRequestFormParams.model_validate(
            {"message": "q", "requestedSchema": APPROVAL_SCHEMA, "_meta": meta})
        for _ in range(2):
            answer = await self.bridge.approvals.elicit(SimpleNamespace(meta=None), ask)
            assert answer.action == "accept"
            await asyncio.sleep(self.gap)
        return types.CallToolResult(content=[types.TextContent(type="text", text="done")])


def two_questions(holder, gap):
    class Conn(_StubConn):
        async def session(self, timeout):
            return _TwoQuestions(holder[0], gap)
    return Conn


async def test_a_second_question_is_not_answered_by_the_first(tmp_path):
    holder = []
    b = Bridge(tmp_path, connection_factory=two_questions(holder, 0.3))
    holder.append(b)
    await b.start()
    try:
        first = (await b.handle(click()))["approval_required"]
        # this answer's wait ends before the second question arrives
        short = await b.handle({"op": "approve", "id": first["id"], "timeout": 0.1})
        assert short["error"]["code"] == "timeout"
        await until(lambda: len(b.approvals.pending()) == 1)
        [second] = b.approvals.pending()
        assert second["id"] != first["id"]
        reply = await asyncio.wait_for(b.handle({"op": "approve", "id": second["id"]}), 5)
        assert reply["ok"] and reply["result"]["content"][0]["text"] == "done"
    finally:
        await b.stop()


async def test_a_follow_up_question_is_reported_by_the_answer(tmp_path):
    holder = []
    b = Bridge(tmp_path, connection_factory=two_questions(holder, 0))
    holder.append(b)
    await b.start()
    try:
        first = (await b.handle(click()))["approval_required"]
        second = (await asyncio.wait_for(
            b.handle({"op": "approve", "id": first["id"]}), 5))["approval_required"]
        assert second["id"] != first["id"]
        reply = await asyncio.wait_for(b.handle({"op": "approve", "id": second["id"]}), 5)
        assert reply["result"]["content"][0]["text"] == "done"
    finally:
        await b.stop()


async def test_after_the_answer_the_call_gets_its_own_timeout_again(tmp_path):
    # The question postponed the deadline to hold + timeout; the answer resets it to timeout
    # from now, so a call that then hangs is not left running for the rest of the hold.
    holder = []
    b = Bridge(tmp_path, connection_factory=two_questions(holder, 30), approval_hold=30)
    holder.append(b)
    await b.start()
    try:
        first = (await b.handle(click(timeout=0.5)))["approval_required"]
        started = asyncio.get_running_loop().time()
        reply = await asyncio.wait_for(b.handle({"op": "approve", "id": first["id"]}), 5)
        assert reply["error"]["code"] == "timeout"
        assert asyncio.get_running_loop().time() - started < 2
    finally:
        await b.stop()


async def test_a_raised_flag_with_an_answered_question_is_not_news(tmp_path):
    # Pins the guard the test above may pass without (it depends on which task the loop wakes
    # first): a flag left set by a question that has since been answered must not come back
    # as approval_required for that answered question.
    b = Bridge(tmp_path, connection_factory=lambda: None)
    call = b.approvals.open_call("browser_click")
    answered = asyncio.get_running_loop().create_future()
    answered.set_result(None)
    call.approval = Approval(id="0badc0de", message="q", expires_at=b.approvals.now() + 50,
                             answer=answered, call=call)
    call.asked.set()
    call.task = asyncio.create_task(asyncio.sleep(5))
    try:
        with pytest.raises(Exception) as caught:
            await b._follow(call, 0.2)
        assert caught.value.reply["error"]["code"] == "timeout"
    finally:
        call.task.cancel()
