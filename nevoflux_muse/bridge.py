"""The resident bridge: one HeadConnection, answered over the local socket.

`nf` runs once per agent turn; deriving the channel key and initializing an MCP
session every time would cost seconds, so the bridge keeps both alive and `nf`
asks it to act (main design D8). handle() is the whole protocol and never
touches a socket, so it is tested on every platform; serve() is the thin
AF_UNIX layer around it.

At most max_in_flight calls run at once (the head buffers 32 messages per
session); waiting for a slot counts toward the call's timeout. A call runs as
its own task: when the head asks for an approval (approvals.py) `call`
returns approval_required at once and the call keeps waiting, with its slot,
until `approve` or `deny` answers; the approval wait does not count toward its
timeout. The log names the tool, the outcome and the time taken, never
arguments, results or approval messages.

A delegated task's tools (tasks.py) are ordinary calls. The bridge reads every
nevoflux_task_wait result for the approvals it lists, so `approve`/`deny` with
such an approval id (a UUID) can answer it with nevoflux_task_approve; an
8-hex id is one of our own elicitation questions, as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from mcp import MCPError
from mcp_types import CONNECTION_CLOSED

from . import __version__, auth, ipc, pairing, state, tasks
from .approvals import APPROVAL_HOLD, APPROVAL_WINDOW, CALL_META, Approvals, Call, Deadline
from .session import HeadConnection

log = logging.getLogger(__name__)


class _Fail(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.reply = ipc.error_reply(code, message, **extra)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _seconds(msg: dict, key: str, default: float) -> float:
    value = msg.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise _Fail("bad_request", f"{key} must be a positive number of seconds")
    return float(value)


class Bridge:
    def __init__(self, d: Path, *,
                 connection_factory: Callable[[], HeadConnection | None] | None = None,
                 max_in_flight: int = ipc.MAX_IN_FLIGHT, connect_wait: float = ipc.CONNECT_WAIT,
                 call_timeout: float = ipc.CALL_TIMEOUT,
                 approval_window: float = APPROVAL_WINDOW, approval_hold: float = APPROVAL_HOLD):
        self.d = d
        self._factory = connection_factory or self._connection_from_files
        self._connect_wait = connect_wait
        self._call_timeout = call_timeout
        self._slots = asyncio.Semaphore(max_in_flight)
        self.approvals = Approvals(window=approval_window, hold=approval_hold)
        self.task_approvals = tasks.ApprovalIndex()  # outlives reloads: the head keeps them
        self._running: set[asyncio.Task] = set()  # call tasks, taken down by stop and reload
        self._conn: HeadConnection | None = None
        self._load_error: str | None = None
        self._since = _now()
        self._stopping = asyncio.Event()
        self._lock = asyncio.Lock()  # start/stop/reload never interleave

    def _connection_from_files(self) -> HeadConnection | None:
        found = pairing.load_pairing(self.d)
        account = state.read_state(self.d, state.ACCOUNT_FILE, auth.ACCOUNT_FIELDS)
        if found is None or account is None:
            return None
        return HeadConnection(found, auth.token_provider(self.d))

    @property
    def state(self) -> str:
        return self._conn.state if self._conn is not None else "not_ready"

    async def start(self) -> None:
        async with self._lock:
            await self._start()

    async def _start(self) -> None:
        try:
            conn = self._factory()
            self._load_error = None
        except (state.UnsupportedState, OSError) as e:
            conn, self._load_error = None, str(e)
        except Exception as e:  # noqa: BLE001 - its text is not ours to repeat
            conn, self._load_error = None, f"unexpected {type(e).__name__}"
            log.error("loading the connection failed: %s", type(e).__name__)
        if conn is not None:
            conn.elicitation_callback = self.approvals.elicit
            await conn.__aenter__()
        self._conn = conn
        self._since = _now()
        log.info("connection %s", "started" if conn is not None else "not ready")

    async def stop(self) -> None:
        async with self._lock:
            await self._stop()

    async def _stop(self) -> None:
        # Take down every call this connection started, waiting for an approval or not:
        # none outlives it in the background (their callers get connection_lost). Open
        # questions are answered "cancel" first, so an `nf approve` that lands while the
        # calls wind down finds nothing to approve.
        self.approvals.cancel_open()
        running = list(self._running)
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        conn, self._conn = self._conn, None
        if conn is not None:
            await conn.__aexit__(None, None, None)

    async def reload(self) -> None:
        async with self._lock:
            await self._stop()
            await self._start()

    def request_stop(self) -> None:
        self._stopping.set()

    async def wait_stopped(self) -> None:
        await self._stopping.wait()

    def status(self) -> dict:
        conn = self._conn
        error = conn.error if conn is not None else None
        if error is None:
            last = self._load_error
        elif conn.state == "error":
            last = f"unexpected {type(error).__name__}"  # its text is not ours to repeat
        else:
            last = str(error)
        return {"state": self.state, "since": self._since, "pid": os.getpid(),
                "head": conn.head if conn is not None else None, "last_error": last}

    async def handle(self, msg: object) -> dict:
        if not isinstance(msg, dict) or not isinstance(msg.get("op"), str):
            return ipc.error_reply("bad_request", "a request is a JSON object with an op")
        op = msg["op"]
        try:
            if op == "ping":
                return {"ok": True, "pid": os.getpid(), "version": __version__}
            if op == "status":
                return {"ok": True, **self.status()}
            if op == "tools":
                return await self._tools(msg)
            if op == "call":
                return await self._call(msg)
            if op == "approve":
                return await self._answer(msg, approve=True)
            if op == "deny":
                return await self._answer(msg, approve=False)
            if op == "pending":
                return {"ok": True, "pending": self.approvals.pending()}
            if op == "reload":
                await self.reload()
                return {"ok": True}
            if op == "shutdown":
                return {"ok": True}  # serve() stops after the reply is written
        except _Fail as f:
            return f.reply
        return ipc.error_reply("unknown_op", f"unknown op {op!r}")

    async def _session(self, msg: dict):
        conn = self._conn
        if conn is None:
            raise _Fail("not_connected", self._load_error or "not paired or not signed in",
                        state="not_ready")
        wait = _seconds(msg, "connect_wait", self._connect_wait)
        try:
            return await conn.session(timeout=wait)
        except asyncio.TimeoutError:
            raise _Fail("not_connected", f"not connected to the browser ({conn.state})",
                        state=conn.state) from None
        except Exception:  # noqa: BLE001 - a fatal state: say which, in our own words
            raise _Fail("not_connected", self.status()["last_error"] or conn.state,
                        state=conn.state) from None

    async def _guarded(self, label: str, deadline: Deadline, make: Callable[[], Awaitable]):
        started = time.monotonic()
        outcome = "ok"

        async def run():
            async with self._slots:
                return await make()

        inner = asyncio.ensure_future(run())
        deadline.watch(inner)
        try:
            return await inner
        except asyncio.CancelledError:
            if not deadline.expired:  # our caller was cancelled: take the call down too
                inner.cancel()
                outcome = "cancelled"
                raise
            outcome = "timeout"
            raise _Fail("timeout",
                        f"{label} did not finish within {deadline.seconds:g}s") from None
        except MCPError as e:
            if e.code == CONNECTION_CLOSED:
                outcome = "connection_lost"
                raise _Fail("connection_lost",
                            "the session with the browser ended before the answer came") from None
            outcome = "mcp_error"
            raise _Fail("mcp_error", e.message,
                        data={"code": e.code, "message": e.message, "data": e.data}) from None
        except _Fail:
            raise
        except Exception as e:  # noqa: BLE001 - its text may hold a result: name the type only
            outcome = "internal"
            raise _Fail("internal", f"unexpected {type(e).__name__}") from None
        finally:
            deadline.stop()
            log.info("%s -> %s in %.2fs", label, outcome, time.monotonic() - started)

    async def _tools(self, msg: dict) -> dict:
        s = await self._session(msg)
        result = await self._guarded("tools/list", Deadline(self._call_timeout), s.list_tools)
        return {"ok": True, "tools": [
            {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
            for t in result.tools]}

    async def _call(self, msg: dict) -> dict:
        tool, args = msg.get("tool"), msg.get("args", {})
        if not isinstance(tool, str) or not tool or not isinstance(args, dict):
            raise _Fail("bad_request", "call needs a tool name and an args object")
        timeout = _seconds(msg, "timeout", self._call_timeout)
        s = await self._session(msg)
        call = self.approvals.open_call(tool)
        call.deadline = Deadline(timeout)
        call.task = asyncio.create_task(self._run_call(call, s, args))
        self._running.add(call.task)

        def done(task: asyncio.Task) -> None:
            self._running.discard(task)
            self.approvals.close_call(call)

        call.task.add_done_callback(done)
        return await self._follow(call)

    async def _run_call(self, call: Call, s, args: dict) -> dict:
        try:
            result = await self._guarded(
                f"call {call.tool!r}", call.deadline,
                lambda: s.call_tool(call.tool, args, meta={CALL_META: call.id}))
            try:
                dumped = result.model_dump(mode="json", by_alias=True, exclude_none=True)
            except Exception as e:  # noqa: BLE001 - name the type only
                raise _Fail("internal", f"unexpected {type(e).__name__}") from None
            if call.tool == tasks.WAIT:
                self.task_approvals.learn(dumped)
            return {"ok": True, "result": dumped}
        except _Fail as f:
            return f.reply

    async def _follow(self, call: Call, wait: float | None = None) -> dict:
        """The call's reply, or approval_required as soon as the head asks about it."""
        loop = asyncio.get_running_loop()
        until = None if wait is None else loop.time() + wait
        while True:
            asked = asyncio.ensure_future(call.asked.wait())
            try:
                left = None if until is None else max(0.0, until - loop.time())
                await asyncio.wait({call.task, asked}, timeout=left,
                                   return_when=asyncio.FIRST_COMPLETED)
            finally:
                asked.cancel()
            if call.task.done():
                if call.task.cancelled():  # stop or reload took it down
                    raise _Fail("connection_lost",
                                "the bridge stopped or reloaded before the answer came")
                return call.task.result()
            if call.asked.is_set():
                call.asked.clear()
                approval = call.approval
                # An answered question is not news: a question asked while nobody followed
                # the call leaves the flag set, and its answer is what brought us here.
                if approval is not None and not approval.answer.done():
                    return {"ok": True,
                            "approval_required": approval.public(self.approvals.now())}
                continue  # answered meanwhile: keep waiting for the result
            raise _Fail("timeout", f"call {call.tool!r} did not finish within {wait:g}s "
                                   "of the answer")

    async def _answer(self, msg: dict, approve: bool) -> dict:
        approval_id = msg.get("id")
        if not isinstance(approval_id, str) or not approval_id:
            raise _Fail("bad_request", "approve and deny need the approval id")
        wait = _seconds(msg, "timeout", self._call_timeout)
        if tasks.is_task_id(approval_id):
            return await self._answer_task(msg, approval_id, approve, wait)
        if "task" in msg or "answer" in msg:
            raise _Fail("bad_request", "task and answer only go with a delegated task's approval")
        approval = self.approvals.answer(approval_id, approve)
        if approval is None:
            raise _Fail("approval_expired", "that approval is no longer open: it expired, was "
                                            "already answered, or the bridge restarted")
        call = approval.call
        if call is None or call.task is None:
            return {"ok": True, "answered": "approved" if approve else "denied"}
        return await self._follow(call, wait)

    async def _answer_task(self, msg: dict, approval_id: str, approve: bool,
                           wait: float) -> dict:
        """Answer a delegated task's approval with nevoflux_task_approve (M3 contract §1)."""
        task_id = msg.get("task")
        if task_id is None:
            task_id = self.task_approvals.task_of(approval_id)
            if task_id is None:
                raise _Fail("task_required",
                            f"no task wait this bridge saw listed approval {approval_id}; "
                            "pass the task id with --task")
        elif not tasks.is_task_id(task_id):
            raise _Fail("bad_request", "task must be a task id")
        answer = msg.get("answer")
        if answer is not None:  # the daemon's checks and wording, in its order
            if not isinstance(answer, str):
                raise _Fail("bad_request", "answer must be a string")
            if len(answer) > tasks.ANSWER_MAX:  # characters, as the daemon counts
                raise _Fail("bad_request",
                            f"answer must be at most {tasks.ANSWER_MAX} characters")
            if not approve:
                raise _Fail("bad_request", "answer is only allowed with allow")
        # The daemon matches ids case-sensitively (ruling C-F5); never allow_always (C8).
        args = {"task_id": task_id.lower(), "approval_id": approval_id.lower(),
                "choice": "allow" if approve else "deny"}
        if answer is not None:
            args["answer"] = answer
        return await self._call({"op": "call", "tool": tasks.APPROVE, "args": args,
                                 "timeout": wait})


async def serve(bridge: Bridge, path: Path):
    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        msg = None
        try:
            msg = await ipc.read_request(reader)
            if msg is None:
                reply = ipc.error_reply("bad_request", "a request is one line of JSON")
            else:
                reply = await bridge.handle(msg)
            await ipc.write_reply(writer, reply)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as e:  # noqa: BLE001 - last resort; never log the text or traceback
            log.error("serving a client failed: %s", type(e).__name__)
            with contextlib.suppress(Exception):
                await ipc.write_reply(
                    writer, ipc.error_reply("internal", f"unexpected {type(e).__name__}"))
        finally:
            if isinstance(msg, dict) and msg.get("op") == "shutdown":
                bridge.request_stop()  # whether or not the reply got through
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    return await asyncio.start_unix_server(on_client, path=str(path), limit=ipc.MAX_LINE)
