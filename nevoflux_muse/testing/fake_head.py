"""A head test double for the agent channel.

Mirrors the head (protocol §4-§5, and the stub head's tool table):
  - greets with a fresh challenge whenever the relay's peer count rises, and
    on rechallenge() (a head restart)
  - accepts only c2h frames under the current challenge with a strictly
    increasing counter starting at 0, and an object m
  - answers initialize with serverInfo.name "nevoflux-head" and
    capabilities.experimental.nevoflux.protocol; tools/list from its table;
    tools/call of a listed tool with a text result, of another whitelisted
    tool with -32602 "unavailable", of anything else with "not_allowed"
  - test knobs: ignore_initialize / fail_initialize swallow or reject that many
    initialize requests

Known deviations from the real head (do not rely on these here):
  - no bad-frame limit (the head drops the session after 8 refused frames)
  - no 32-message inbound buffer
  - serverInfo.version is "0.0.0-fake"; tool results are fixed text
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os

import websockets

from ..transport import Peers, RelayLink

WHITELIST = (
    "browser_snapshot", "browser_get_markdown", "browser_screenshot", "browser_get_tabs",
    "browser_navigate", "browser_activate_tab", "browser_click_by_id", "browser_fill_by_id",
    "browser_type_by_id", "browser_click", "browser_fill", "browser_type", "browser_key_press",
    "browser_scroll", "browser_wait_for", "browser_upload_file",
)


class FakeHead:
    def __init__(self, relay_url: str, channel: str, key: bytes, *, token: str = "test",
                 protocol: int = 1, tools: tuple[str, ...] = ("browser_snapshot", "browser_navigate"),
                 hang: tuple[str, ...] = ("hang",), silent: bool = False,
                 ignore_initialize: int = 0, fail_initialize: int = 0):
        self.relay_url, self.channel, self.key, self.token = relay_url, channel, key, token
        self.protocol, self.tools, self.hang, self.silent = protocol, tools, hang, silent
        self.ignore_initialize, self.fail_initialize = ignore_initialize, fail_initialize
        self.requests: list[dict] = []
        self.arrivals = 0
        self._ch: str | None = None
        self._out_n = 0
        self._last_in = -1
        self._challenges: list[dict] = []
        self._link: RelayLink | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> FakeHead:
        self._link = await RelayLink.connect(self.relay_url, self.channel, self.token, self.key)
        self._task = asyncio.create_task(self._loop())
        return self

    async def stop(self) -> None:
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
        frame = {"d": "h2c", "n": self._out_n, "ch": self._ch, "m": m}
        self._out_n += 1
        await self._link.send({"k": "frame", "frame": frame})

    async def _handle(self, m: dict) -> None:
        self.requests.append(m)
        method, mid = m.get("method"), m.get("id")
        if method is None or mid is None:
            return  # a notification or a response
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
            result = {
                "protocolVersion": params.get("protocolVersion", "2025-11-25"),
                "capabilities": {"tools": {}, "experimental": {"nevoflux": {"protocol": self.protocol}}},
                "serverInfo": {"name": "nevoflux-head", "version": "0.0.0-fake"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": t, "inputSchema": {"type": "object"}} for t in self.tools]}
        elif method == "tools/call":
            name = params.get("name")
            if name in self.hang:
                return
            if name not in self.tools:
                code = "unavailable" if name in WHITELIST else "not_allowed"
                await self._send({"jsonrpc": "2.0", "id": mid, "error": {
                    "code": -32602, "message": f"{name}: {code}", "data": {"code": code}}})
                return
            result = {"content": [{"type": "text", "text": f"fake {name}"}]}
        else:
            await self._send({"jsonrpc": "2.0", "id": mid,
                              "error": {"code": -32601, "message": "method not found"}})
            return
        await self._send({"jsonrpc": "2.0", "id": mid, "result": result})
