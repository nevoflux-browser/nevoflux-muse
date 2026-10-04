"""A relay test double with the real relay's semantics.

Mirrors nevoflux-portal/workers/portal-relay/src/relay-logic.ts:
  - presence counts exclude the socket being told (relayTargets)
  - a newcomer hears the count once; everyone already there hears the new
    count; on a departure the survivors hear theirs
  - a message goes to everyone else on the channel, bytes untouched; with
    nobody else there, the sender is told {"k":"peers","n":0}
  - nothing is stored

The prototype counted the listener itself, which made a lone head look like it
had company. Clients built against that would misread the real relay.
"""

from __future__ import annotations

import json
import urllib.parse

import websockets


def _notice(n: int) -> str:
    return json.dumps({"k": "peers", "n": n})


class FakeRelay:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, expect_token: str = ""):
        self.host = host
        self.port = port
        self.expect_token = expect_token
        self._channels: dict[str, set] = {}
        self._server = None

    async def _tell(self, ws, n: int) -> None:
        try:
            await ws.send(_notice(n))
        except websockets.ConnectionClosed:
            pass

    async def _handler(self, ws) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(ws.request.path).query)
        channel = (query.get("c") or [""])[0]
        token = (query.get("t") or [""])[0]
        if not channel or (self.expect_token and token != self.expect_token):
            await ws.close(code=4403, reason="forbidden")
            return

        members = self._channels.setdefault(channel, set())
        others = list(members)
        members.add(ws)
        await self._tell(ws, len(others))
        for peer in others:
            await self._tell(peer, len(members) - 1)

        try:
            async for msg in ws:
                targets = [p for p in members if p is not ws]
                if not targets:
                    await self._tell(ws, 0)
                    continue
                for peer in targets:
                    try:
                        await peer.send(msg)
                    except websockets.ConnectionClosed:
                        pass
        finally:
            members.discard(ws)
            if not members:
                self._channels.pop(channel, None)
            else:
                for peer in list(members):
                    await self._tell(peer, len(members) - 1)

    async def start(self) -> FakeRelay:
        self._server = await websockets.serve(self._handler, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
