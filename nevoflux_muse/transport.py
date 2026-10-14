"""One WebSocket to the relay (protocol §2, §3).

The relay sends plaintext {"k":"peers","n":N} notices (N counts the other
sockets, never this one); everything else is binary and sealed under the
channel key. Anything that does not read as one of those is dropped, as the
protocol requires; that includes the relay's "pong".

Every 30 s the link sends the text "ping" the relay answers by itself. The
relay closes a socket that has sent it and then gone quiet, so this end stops
being counted as attached soon after it drops off the network, instead of a
quarter of an hour later. The dial URL carries the account JWT, so neither it nor
the library's exception text (which can quote it) ever reaches a message.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import urllib.parse
from dataclasses import dataclass

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from . import crypto

MAX_MESSAGE = 64 * 1024 * 1024  # screenshots travel as base64
KEEPALIVE_PING = "ping"  # must match KEEPALIVE_PING in the portal relay, byte for byte
KEEPALIVE_INTERVAL = 30.0
_TOKEN_PARAM = re.compile(r"([?&]t=)[^&\s]*")


class _RedactToken(logging.Filter):
    """The library logs the request line at DEBUG, JWT included."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _TOKEN_PARAM.sub(r"\1<redacted>", record.getMessage())
        record.args = None
        return True


_ws_log = logging.getLogger("nevoflux_muse.relay.ws")
_ws_log.addFilter(_RedactToken())


@dataclass(frozen=True)
class Peers:
    n: int


class RelayRefused(Exception):
    """The relay said no before the upgrade: 400 channel, 401 token, 403 another account."""

    def __init__(self, status: int):
        super().__init__(f"the relay refused the connection (HTTP {status})")
        self.status = status


class RelayUnreachable(Exception):
    pass


class RelayLink:
    def __init__(self, ws: ClientConnection, key: bytes, *,
                 keepalive: float = KEEPALIVE_INTERVAL):
        self.ws = ws
        self._key = key
        self._beat = asyncio.create_task(self._keepalive(keepalive))

    async def _keepalive(self, every: float) -> None:
        while True:
            await asyncio.sleep(every)
            try:
                await self.ws.send(KEEPALIVE_PING)
            except ConnectionClosed:
                return

    @classmethod
    async def connect(cls, relay: str, channel: str, token: str, key: bytes, *,
                      open_timeout: float = 10,
                      keepalive: float = KEEPALIVE_INTERVAL) -> RelayLink:
        url = f"{relay}/?{urllib.parse.urlencode({'c': channel, 't': token})}"
        try:
            ws = await connect(url, open_timeout=open_timeout, max_size=MAX_MESSAGE,
                               logger=_ws_log)
        except InvalidStatus as e:
            raise RelayRefused(e.response.status_code) from None
        except Exception as e:  # noqa: BLE001 - its text may quote the URL
            raise RelayUnreachable(f"cannot reach the relay ({type(e).__name__})") from None
        return cls(ws, key, keepalive=keepalive)

    async def recv(self) -> Peers | dict:
        while True:
            msg = await self.ws.recv()
            if isinstance(msg, str):
                try:
                    obj = json.loads(msg)
                except ValueError:
                    continue
                if not isinstance(obj, dict) or obj.get("k") != "peers":
                    continue
                n = obj.get("n")
                if isinstance(n, int) and not isinstance(n, bool):
                    return Peers(n)
                continue
            opened = crypto.open_sealed(self._key, msg)
            if opened is not None:
                return opened

    async def send(self, msg: dict) -> None:
        await self.ws.send(crypto.seal(self._key, msg))

    async def close(self) -> None:
        self._beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._beat
        await self.ws.close()
