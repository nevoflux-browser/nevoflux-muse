"""The resident bridge: one HeadConnection, answered over the local socket.

`nf` runs once per agent turn; deriving the channel key and initializing an MCP
session every time would cost seconds, so the bridge keeps both alive and `nf`
asks it to act (main design D8). handle() is the whole protocol and never
touches a socket, so it is tested on every platform; serve() is the thin
AF_UNIX layer around it.

At most max_in_flight calls run at once (the head buffers 32 messages per
session); waiting for a slot counts toward the call's timeout. The log names
the tool, the outcome and the time taken, never arguments or results.
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

from . import __version__, auth, ipc, pairing, state
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
                 call_timeout: float = ipc.CALL_TIMEOUT):
        self.d = d
        self._factory = connection_factory or self._connection_from_files
        self._connect_wait = connect_wait
        self._call_timeout = call_timeout
        self._slots = asyncio.Semaphore(max_in_flight)
        self._conn: HeadConnection | None = None
        self._load_error: str | None = None
        self._since = _now()
        self._stopping = asyncio.Event()

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
        try:
            conn = self._factory()
            self._load_error = None
        except (state.UnsupportedState, OSError) as e:
            conn, self._load_error = None, str(e)
        if conn is not None:
            await conn.__aenter__()
        self._conn = conn
        self._since = _now()
        log.info("connection %s", "started" if conn is not None else "not ready")

    async def stop(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await conn.__aexit__(None, None, None)

    async def reload(self) -> None:
        await self.stop()
        await self.start()

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

    async def _guarded(self, label: str, timeout: float, make: Callable[[], Awaitable]):
        started = time.monotonic()
        outcome = "ok"

        async def run():
            async with self._slots:
                return await make()

        try:
            return await asyncio.wait_for(run(), timeout)
        except asyncio.TimeoutError:
            outcome = "timeout"
            raise _Fail("timeout", f"{label} did not finish within {timeout:g}s") from None
        except MCPError as e:
            if e.code == CONNECTION_CLOSED:
                outcome = "connection_lost"
                raise _Fail("connection_lost",
                            "the session with the browser ended before the answer came") from None
            outcome = "mcp_error"
            raise _Fail("mcp_error", e.message,
                        data={"code": e.code, "message": e.message, "data": e.data}) from None
        finally:
            log.info("%s -> %s in %.2fs", label, outcome, time.monotonic() - started)

    async def _tools(self, msg: dict) -> dict:
        s = await self._session(msg)
        result = await self._guarded("tools/list", self._call_timeout, s.list_tools)
        return {"ok": True, "tools": [
            {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
            for t in result.tools]}

    async def _call(self, msg: dict) -> dict:
        tool, args = msg.get("tool"), msg.get("args", {})
        if not isinstance(tool, str) or not tool or not isinstance(args, dict):
            raise _Fail("bad_request", "call needs a tool name and an args object")
        timeout = _seconds(msg, "timeout", self._call_timeout)
        s = await self._session(msg)
        result = await self._guarded(f"call {tool}", timeout, lambda: s.call_tool(tool, args))
        return {"ok": True,
                "result": result.model_dump(mode="json", by_alias=True, exclude_none=True)}


async def serve(bridge: Bridge, path: Path):
    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            msg = await ipc.read_request(reader)
            if msg is None:
                reply = ipc.error_reply("bad_request", "a request is one line of JSON")
            else:
                reply = await bridge.handle(msg)
            await ipc.write_reply(writer, reply)
            if isinstance(msg, dict) and msg.get("op") == "shutdown":
                bridge.request_stop()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    return await asyncio.start_unix_server(on_client, path=str(path), limit=ipc.MAX_LINE)
