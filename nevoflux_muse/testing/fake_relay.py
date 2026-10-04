"""A relay test double with the production relay's semantics.

Mirrors the production relay:
  - refusals are plain HTTP before the WebSocket upgrade: 400 for a missing
    channel, 401 for a missing/empty/wrong token, 403 when another account
    already owns the channel (the first token to open it owns it)
  - presence counts exclude the socket being told (relayTargets)
  - a newcomer hears the count once; everyone already there hears the new
    count; on a departure the survivors hear theirs
  - a message goes to everyone else on the channel, bytes untouched; with
    nobody else there, the sender is told {"k":"peers","n":0}
  - nothing is stored

The prototype counted the listener itself, which made a lone head look like it
had company. Clients built against that would misread the real relay.

Known deviations from the production relay (do not rely on these here):
  - no per-channel capacity: production holds 4 sockets and closes the oldest
    with 1013 "channel is full" (which can be the head); this double never does
  - a text "ping" is relayed like any message; production answers "pong"
  - no /presence HTTP probe
  - no JWT/JWKS verification: any accepted token counts as "an account"
"""

from __future__ import annotations

import json
import urllib.parse
from http import HTTPStatus

import websockets
from websockets.asyncio.server import serve


def _notice(n: int) -> str:
    return json.dumps({"k": "peers", "n": n})


class FakeRelay:
    def __init__(
        self, host: str = "127.0.0.1", port: int = 0, expect_token: str | None = None
    ):
        """expect_token=None accepts any non-empty token; a string accepts only that one."""
        self.host = host
        self.port = port
        self.expect_token = expect_token
        self._channels: dict[str, set] = {}
        self._owners: dict[str, str] = {}
        self._server = None

    async def _tell(self, ws, n: int) -> None:
        try:
            await ws.send(_notice(n))
        except websockets.ConnectionClosed:
            pass

    @staticmethod
    def _params(path: str) -> tuple[str, str]:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
        return (query.get("c") or [""])[0], (query.get("t") or [""])[0]

    async def _process_request(self, connection, request):
        channel, token = self._params(request.path)
        if not channel:
            return connection.respond(HTTPStatus.BAD_REQUEST, "missing channel\n")
        if not token or (self.expect_token is not None and token != self.expect_token):
            return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        owner = self._owners.get(channel)
        if owner is not None and owner != token:
            return connection.respond(HTTPStatus.FORBIDDEN, "channel owned by another account\n")
        return None

    async def _handler(self, ws) -> None:
        channel, token = self._params(ws.request.path)
        self._owners.setdefault(channel, token)
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
                self._owners.pop(channel, None)
            else:
                for peer in list(members):
                    await self._tell(peer, len(members) - 1)

    async def start(self) -> FakeRelay:
        self._server = await serve(
            self._handler, self.host, self.port, process_request=self._process_request
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
