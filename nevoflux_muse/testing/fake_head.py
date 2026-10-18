"""A head test double for the agent channel.

Mirrors the head (protocol §4-§5, and the stub head's tool table):
  - greets with a fresh challenge whenever the relay's peer count rises, and
    on rechallenge() (a head restart)
  - accepts only c2h frames under the current challenge with a strictly
    increasing counter starting at 0, and an object m
  - answers initialize with serverInfo.name "nevoflux-head" and
    capabilities.experimental.nevoflux.protocol; tools/list from its table;
    tools/call of a listed tool with a text result, of another whitelisted
    tool with -32602 "unavailable", of anything else with an unknown_tool
    tool error
  - test knobs: ignore_initialize / fail_initialize swallow or reject that many
    initialize requests
  - M2a: tools in `ask` need an approval: the head sends a form elicitation
    (APPROVAL_SCHEMA) and runs the tool only on accept + approve=true; a client
    without the elicitation capability gets approval_denied
    (client_cannot_approve), a JSON-RPC error reply gets approval_denied
    (approval_failed); no answer within approval_timeout sends
    notifications/cancelled and approval_timeout. Its request ids are integers
    from 0 and its message is multi-line (ask_message()), as the real head's.
    Tools in `errors` return that structured tool error (tool_error()). Like
    the real head, every elicitation
    carries _meta.progressToken equal to its request id, and (echo_call_meta, on
    by default) copies the call's _meta["io.nevoflux/call"].
  - M3: tools in `scripted` answer the next result of their list (the last one
    repeats), whitelisted or not, and are listed by tools/list. When the call
    carries _meta.progressToken, the head first sends one notifications/progress
    per entry of `progress[tool]` ({progressToken, progress: 1, 2, ..., message},
    no total, as M3 contract §2.2), sleeping `progress_gap` seconds before each.
    task_result(), task_state() and approval_item() build M3 task tool results.

Known deviations from the real head (do not rely on these here):
  - no bad-frame limit (the head drops the session after 8 refused frames)
  - no 32-message inbound buffer
  - serverInfo.version is "0.0.0-fake"; tool results are fixed text
  - a whitelisted tool missing from `tools` still answers the M1 JSON-RPC error
    "unavailable"; the M2 head answers structured tool errors instead
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os

import websockets

from .. import aio
from ..approvals import APPROVAL_SCHEMA, CALL_META
from ..transport import Peers, RelayLink

WHITELIST = (
    "browser_snapshot", "browser_get_markdown", "browser_screenshot", "browser_get_tabs",
    "browser_navigate", "browser_activate_tab", "browser_click_by_id", "browser_fill_by_id",
    "browser_type_by_id", "browser_click", "browser_fill", "browser_type", "browser_key_press",
    "browser_scroll", "browser_wait_for", "browser_upload_file",
)


def ask_message(name: str) -> str:
    """The approval question, multi-line like the real head's: action, tab, reason."""
    return f"Run {name}\nTab: tab 1\nWhy: test asks"


def tool_error(code: str, message: str, **extra) -> dict:
    """A tool error as the M2 head returns it (M2a spec section 5): content[0].text is the
    structuredContent object as JSON."""
    structured = {"error": {"code": code, "message": message, **extra}}
    return {"content": [{"type": "text", "text": json.dumps(structured)}], "isError": True,
            "structuredContent": structured}


TASK_ID = "6f1c1a52-3c1e-4b8e-9a51-2d0f6c7e9b10"
APPROVAL_ID = "0e8a7d0c-5a43-4f7e-8f55-1b2c3d4e5f60"
SESSION_ID = "9b2d4f60-1a3c-4e5f-8a7b-6c5d4e3f2a10"
JPEG_B64 = base64.b64encode(
    bytes([0xFF, 0xD8, 0xFF, 0xE0]) + bytes(60) + bytes([0xFF, 0xD9])).decode()


def task_result(structured: dict, image: str | None = None) -> dict:
    """A task tool's success as the M3 head returns it (contract R6, R12): content[0].text is
    structuredContent as JSON; a done wait may add one JPEG image block."""
    content = [{"type": "text", "text": json.dumps(structured)}]
    if image is not None:
        content.append({"type": "image", "data": image, "mimeType": "image/jpeg"})
    return {"content": content, "structuredContent": structured, "isError": False}


def task_state(state: str, **extra) -> dict:
    """A nevoflux_task_wait structuredContent (contract §2)."""
    return {"task_id": TASK_ID, "session": SESSION_ID, "state": state, "steps": [], **extra}


_SUMMARY = {"tool": "Submit the form on example.com", "question": "Pick a colour",
            "plan": "Build the calculator"}


def approval_item(kind: str = "tool", approval_id: str = APPROVAL_ID, **extra) -> dict:
    """An approval item of a wait result (contract §2.1)."""
    item = {"approval_id": approval_id, "kind": kind, "summary": _SUMMARY[kind],
            "tool": "browser_click" if kind == "tool" else None, "target": None,
            "choices": ["allow", "deny"], "at": "2026-10-16T10:00:00Z", "expires_in": 1740}
    if kind == "question":
        item.update(question="Which colour?", options=["Blue", "Red"])
    if kind == "plan":
        item.update(plan_steps=["Open the app skill", "Render the Canvas"])
    item.update(extra)
    return item


class FakeHead:
    def __init__(self, relay_url: str, channel: str, key: bytes, *, token: str = "test",
                 protocol: int = 1, tools: tuple[str, ...] = ("browser_snapshot", "browser_navigate"),
                 hang: tuple[str, ...] = ("hang",), silent: bool = False,
                 ignore_initialize: int = 0, fail_initialize: int = 0,
                 ask: tuple[str, ...] = (), approval_timeout: float = 60.0,
                 errors: dict[str, dict] | None = None, echo_call_meta: bool = True,
                 scripted: dict[str, list[dict]] | None = None,
                 progress: dict[str, list[str]] | None = None, progress_gap: float = 0.0):
        self.relay_url, self.channel, self.key, self.token = relay_url, channel, key, token
        self.protocol, self.tools, self.hang, self.silent = protocol, tools, hang, silent
        self.ignore_initialize, self.fail_initialize = ignore_initialize, fail_initialize
        self.ask, self.approval_timeout = ask, approval_timeout
        self.errors, self.echo_call_meta = dict(errors or {}), echo_call_meta
        self.scripted = {name: list(results) for name, results in (scripted or {}).items()}
        self.progress, self.progress_gap = dict(progress or {}), progress_gap
        self.requests: list[dict] = []
        self.sent: list[dict] = []
        self.answers: list[dict] = []
        self.client_caps: dict = {}
        self.arrivals = 0
        self._ch: str | None = None
        self._out_n = 0
        self._last_in = -1
        self._challenges: list[dict] = []
        self._link: RelayLink | None = None
        self._task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._asked = 0
        self._waiting: dict[int, asyncio.Future] = {}
        self._jobs: set[asyncio.Task] = set()

    async def start(self) -> FakeHead:
        self._link = await RelayLink.connect(self.relay_url, self.channel, self.token, self.key)
        self._task = asyncio.create_task(self._loop())
        return self

    async def stop(self) -> None:
        for job in list(self._jobs):
            job.cancel()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._link:
            await self._link.close()

    async def rechallenge(self) -> None:
        await self._challenge()

    async def replay_challenge(self, index: int = -1) -> None:
        """Send an earlier challenge frame again, as a replaying relay would."""
        await self._link.send({"k": "frame", "frame": self._challenges[index]})

    async def send_raw(self, data: bytes | str) -> None:
        await self._link.ws.send(data)

    async def _loop(self) -> None:
        peers = 0
        try:
            while True:
                item = await self._link.recv()
                if isinstance(item, Peers):
                    if item.n > peers:
                        self.arrivals += 1
                        if not self.silent:
                            await self._challenge()
                    peers = item.n
                    continue
                frame = item.get("frame") if item.get("k") == "frame" else None
                if self._accept(frame):
                    await self._handle(frame["m"])
        except websockets.ConnectionClosed:
            pass

    async def _challenge(self) -> None:
        async with self._send_lock:
            self._ch = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()
            self._last_in = -1
            frame = {"d": "h2c", "n": 0, "ch": self._ch, "m": None}
            self._challenges.append(frame)
            self._out_n = 1
            await self._link.send({"k": "frame", "frame": frame})

    def _accept(self, f: object) -> bool:
        if not (isinstance(f, dict) and {"d", "n", "ch", "m"} <= f.keys()):
            return False
        n = f["n"]
        if f["d"] != "c2h" or f["ch"] != self._ch:
            return False
        if not isinstance(n, int) or isinstance(n, bool) or n <= self._last_in:
            return False
        if not isinstance(f["m"], dict):
            return False
        self._last_in = n
        return True

    async def _send(self, m: dict) -> None:
        async with self._send_lock:
            self.sent.append(m)
            frame = {"d": "h2c", "n": self._out_n, "ch": self._ch, "m": m}
            self._out_n += 1
            await self._link.send({"k": "frame", "frame": frame})

    async def _handle(self, m: dict) -> None:
        self.requests.append(m)
        method, mid = m.get("method"), m.get("id")
        if (method is None and isinstance(mid, int) and not isinstance(mid, bool)
                and mid in self._waiting):
            waiting = self._waiting.pop(mid)
            if not waiting.done():
                waiting.set_result(m)
            return
        if method is None or mid is None:
            return  # a notification or another response
        params = m.get("params") or {}
        if method == "initialize":
            if self.ignore_initialize > 0:
                self.ignore_initialize -= 1
                return
            if self.fail_initialize > 0:
                self.fail_initialize -= 1
                await self._send({"jsonrpc": "2.0", "id": mid,
                                  "error": {"code": -32603, "message": "initialize failed"}})
                return
            self.client_caps = params.get("capabilities") or {}
            result = {
                "protocolVersion": params.get("protocolVersion", "2025-11-25"),
                "capabilities": {"tools": {}, "experimental": {"nevoflux": {"protocol": self.protocol}}},
                "serverInfo": {"name": "nevoflux-head", "version": "0.0.0-fake"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            names = self.tools + tuple(n for n in self.scripted if n not in self.tools)
            result = {"tools": [{"name": t, "inputSchema": {"type": "object"}} for t in names]}
        elif method == "tools/call":
            name = params.get("name")
            if name in self.scripted:
                job = asyncio.create_task(self._play(mid, name, params))
                self._jobs.add(job)
                job.add_done_callback(self._jobs.discard)
                return
            if name in self.hang:
                return
            if name not in WHITELIST:
                await self._send({"jsonrpc": "2.0", "id": mid, "result": tool_error(
                    "unknown_tool", f"{name} is not offered")})
                return
            if name not in self.tools:
                await self._send({"jsonrpc": "2.0", "id": mid, "error": {
                    "code": -32602, "message": f"{name}: unavailable",
                    "data": {"code": "unavailable"}}})
                return
            if name in self.errors:
                result = tool_error(**self.errors[name])
            elif name in self.ask:
                job = asyncio.create_task(self._ask_then_answer(mid, name, params))
                self._jobs.add(job)
                job.add_done_callback(self._jobs.discard)
                return
            else:
                result = {"content": [{"type": "text", "text": f"fake {name}"}]}
        else:
            await self._send({"jsonrpc": "2.0", "id": mid,
                              "error": {"code": -32601, "message": "method not found"}})
            return
        await self._send({"jsonrpc": "2.0", "id": mid, "result": result})

    async def _ask_then_answer(self, mid, name: str, params: dict) -> None:
        if "elicitation" not in self.client_caps:
            result = tool_error("approval_denied", "the client cannot ask for approval",
                                reason="client_cannot_approve")
        else:
            eid = self._asked  # an integer from 0, like the real head's request ids
            self._asked += 1
            waiting = asyncio.get_running_loop().create_future()
            self._waiting[eid] = waiting
            meta: dict = {"progressToken": eid}
            call = (params.get("_meta") or {}).get(CALL_META)
            if self.echo_call_meta and call is not None:
                meta[CALL_META] = call
            await self._send({"jsonrpc": "2.0", "id": eid, "method": "elicitation/create",
                              "params": {"mode": "form", "message": ask_message(name),
                                         "requestedSchema": APPROVAL_SCHEMA, "_meta": meta}})
            try:
                reply = await aio.wait_for(waiting, self.approval_timeout)
            except asyncio.TimeoutError:
                self._waiting.pop(eid, None)
                await self._send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                  "params": {"requestId": eid, "reason": "approval timed out"}})
                result = tool_error("approval_timeout", "nobody answered the approval in time")
            else:
                self.answers.append(reply)
                answer = reply.get("result") or {}
                if "error" in reply:
                    result = tool_error("approval_denied",
                                        f"the approval request for {name} failed",
                                        reason="approval_failed")
                elif (answer.get("action") == "accept"
                        and (answer.get("content") or {}).get("approve") is True):
                    result = {"content": [{"type": "text", "text": f"fake {name}"}]}
                else:
                    result = tool_error("approval_denied", "the approval was refused",
                                        reason="declined")
        await self._send({"jsonrpc": "2.0", "id": mid, "result": result})

    async def _play(self, mid, name: str, params: dict) -> None:
        queue = self.scripted[name]
        result = queue.pop(0) if len(queue) > 1 else queue[0]
        token = (params.get("_meta") or {}).get("progressToken")
        if token is not None:
            for n, message in enumerate(self.progress.get(name, ()), 1):
                await asyncio.sleep(self.progress_gap)
                await self._send({"jsonrpc": "2.0", "method": "notifications/progress",
                                  "params": {"progressToken": token, "progress": n,
                                             "message": message}})
        await self._send({"jsonrpc": "2.0", "id": mid, "result": result})
