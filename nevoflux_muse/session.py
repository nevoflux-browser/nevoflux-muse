"""The MCP session with the head, kept up across relay reconnects and head restarts.

HeadConnection dials the relay, waits for the head's challenge, and runs an
mcp ClientSession over the envelope. The SDK talks to a pair of anyio memory
streams; this module moves messages between those streams and the sealed
relay link. Every challenge, the first on a connection or one arriving
mid-connection because the head restarted, starts a fresh session: the old
one is torn down first, so calls still waiting on it fail with
CONNECTION_CLOSED instead of hanging.

While the head is away (the relay counts no peers) the connection waits
rather than redialing; it redials only when the socket drops or a present head
stays silent past the challenge timeout. Errors only a person can fix (a
revoked sign-in, the wrong account, an incompatible head) stop the loop and
are raised by session().
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable

import anyio
import websockets
from mcp import ClientSession, types
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter
from pydantic import ValidationError

from . import PROTOCOL_MAX, PROTOCOL_MIN, __version__
from .auth import AccountError, AuthRevoked
from .envelope import Challenge, Envelope, Message
from .pairing import Pairing
from .transport import Peers, RelayLink, RelayRefused, RelayUnreachable

log = logging.getLogger(__name__)

UNAUTHORIZED_LIMIT = 3
STREAM_BUFFER = 64
TEARDOWN_GRACE = 5.0


class HeadError(Exception):
    """The connection cannot go on until someone fixes something."""


class AccountMismatch(HeadError):
    pass


class RelayRejected(HeadError):
    pass


class IncompatibleHead(HeadError):
    def __init__(self, version: object):
        super().__init__(f"the head speaks agent protocol {version!r}; this client speaks "
                         f"{PROTOCOL_MIN}-{PROTOCOL_MAX}")
        self.version = version


def _protocol(caps: types.ServerCapabilities | None) -> object:
    experimental = (caps.experimental if caps else None) or {}
    nevoflux = experimental.get("nevoflux")
    return nevoflux.get("protocol") if isinstance(nevoflux, dict) else None


class HeadConnection:
    def __init__(self, pairing: Pairing, token_provider: Callable[[], Awaitable[str]], *,
                 challenge_timeout: float = 10.0, open_timeout: float = 10.0,
                 backoff_initial: float = 1.0, backoff_max: float = 60.0):
        self._pairing = pairing
        self._token_provider = token_provider
        self._challenge_timeout = challenge_timeout
        self._open_timeout = open_timeout
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self.state = "connecting"
        self._session: ClientSession | None = None
        self._fatal: Exception | None = None
        self._changed = asyncio.Event()
        self._seen: set[str] = set()  # every challenge ever accepted, across connections
        self._read_w = None
        self._stop: asyncio.Event | None = None
        self._session_task: asyncio.Task | None = None
        self._went_live = False
        self._incompatible: IncompatibleHead | None = None
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> HeadConnection:  # noqa: PYI034 - Self needs 3.11
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc) -> None:
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def session(self, timeout: float | None = None) -> ClientSession:
        """The current initialized session; waits for one, or raises what stopped the loop."""

        async def wait() -> ClientSession:
            while True:
                if self._fatal is not None:
                    raise self._fatal
                if self._session is not None:
                    return self._session
                await self._changed.wait()

        return await asyncio.wait_for(wait(), timeout)

    def _set(self, state: str, session: ClientSession | None = None,
             fatal: Exception | None = None) -> None:
        self.state, self._session, self._fatal = state, session, fatal
        self._changed.set()
        self._changed = asyncio.Event()

    async def _run(self) -> None:
        delay = self._backoff_initial
        refused = 0
        while True:
            self._went_live = False
            try:
                token = await self._token_provider()
                link = await RelayLink.connect(self._pairing.relay, self._pairing.channel, token,
                                               self._pairing.key, open_timeout=self._open_timeout)
            except AuthRevoked as e:
                self._set("auth_revoked", fatal=e)
                return
            except RelayRefused as e:
                if e.status == 401:
                    refused += 1
                    if refused >= UNAUTHORIZED_LIMIT:
                        self._set("auth_revoked", fatal=AuthRevoked(
                            "the relay refused the account token three times in a row; "
                            "run `nf auth begin --reset`"))
                        return
                elif e.status == 403:
                    self._set("account_mismatch", fatal=AccountMismatch(
                        "another NevoFlux account owns this channel; sign in with the account "
                        "the browser uses (`nf auth begin --reset`)"))
                    return
                else:
                    self._set("relay_refused", fatal=RelayRejected(
                        f"the relay refused the channel (HTTP {e.status}); pair again"))
                    return
            except (AccountError, RelayUnreachable) as e:
                log.warning("cannot connect: %s", e)
            else:
                refused = 0
                try:
                    await self._serve(link)
                finally:
                    await link.close()
                if self._incompatible is not None:
                    self._set("incompatible", fatal=self._incompatible)
                    return
            if self._went_live:
                delay = self._backoff_initial
            self._set("connecting")
            await asyncio.sleep(delay * random.uniform(0.5, 1.0))
            delay = min(delay * 2, self._backoff_max)

    async def _serve(self, link: RelayLink) -> None:
        env = Envelope(self._seen)
        loop = asyncio.get_running_loop()
        deadline: float | None = None
        try:
            while True:
                timeout = None if deadline is None else max(0.0, deadline - loop.time())
                try:
                    item = await asyncio.wait_for(link.recv(), timeout)
                except asyncio.TimeoutError:
                    log.info("no challenge within %ss; redialing", self._challenge_timeout)
                    return
                except websockets.ConnectionClosed:
                    return
                if isinstance(item, Peers):
                    if item.n == 0:
                        await self._drop_session()
                        self._set("head_offline")
                        deadline = None
                    elif self._session_task is None and deadline is None:
                        deadline = loop.time() + self._challenge_timeout
                    continue
                if item.get("k") != "frame":
                    continue
                result = env.inbound(item.get("frame"))
                if isinstance(result, Challenge):
                    deadline = None
                    await self._drop_session()
                    self._start_session(link, env)
                elif isinstance(result, Message):
                    await self._deliver(result.m)
                else:
                    log.debug("dropped a frame: %s", result.reason)
        finally:
            await self._drop_session()

    def _start_session(self, link: RelayLink, env: Envelope) -> None:
        read_w, read_r = anyio.create_memory_object_stream[SessionMessage | Exception](STREAM_BUFFER)
        write_w, write_r = anyio.create_memory_object_stream[SessionMessage](STREAM_BUFFER)
        self._read_w = read_w
        self._stop = asyncio.Event()
        self._session_task = asyncio.create_task(
            self._session_main(link, env, read_r, write_w, write_r, self._stop))

    async def _session_main(self, link, env, read_r, write_w, write_r, stop) -> None:
        async def pump() -> None:
            async with write_r:
                async for sm in write_r:
                    m = sm.message.model_dump(by_alias=True, exclude_unset=True, mode="json")
                    try:
                        await link.send({"k": "frame", "frame": env.outbound(m)})
                    except websockets.ConnectionClosed:
                        return

        info = types.Implementation(name="nevoflux-muse", version=__version__)
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(pump)
                try:
                    async with ClientSession(read_r, write_w, client_info=info) as s:
                        await s.initialize()
                        version = _protocol(s.server_capabilities)
                        if not (isinstance(version, int) and not isinstance(version, bool)
                                and PROTOCOL_MIN <= version <= PROTOCOL_MAX):
                            self._incompatible = IncompatibleHead(version)
                            await link.close()
                            return
                        if self._stop is not stop:
                            return  # torn down while initialize was finishing
                        self._went_live = True
                        self._set("connected", session=s)
                        await stop.wait()
                finally:
                    tg.cancel_scope.cancel()
        except Exception as e:  # noqa: BLE001 - the head went away mid-initialize, etc.
            log.debug("MCP session ended: %r", e)

    async def _deliver(self, m: dict) -> None:
        if self._read_w is None:
            return
        try:
            msg = jsonrpc_message_adapter.validate_python(m, by_name=False)
        except ValidationError:
            log.debug("dropped a message that is not JSON-RPC")
            return
        with contextlib.suppress(anyio.ClosedResourceError, anyio.BrokenResourceError):
            await self._read_w.send(SessionMessage(msg))

    async def _drop_session(self) -> None:
        task, read_w, stop = self._session_task, self._read_w, self._stop
        self._session_task = self._read_w = self._stop = None
        if self.state == "connected":
            self._set("connecting")
        if read_w is not None:
            await read_w.aclose()  # waiting calls fail with CONNECTION_CLOSED
        if stop is not None:
            stop.set()
        if task is not None:
            done, _ = await asyncio.wait({task}, timeout=TEARDOWN_GRACE)
            if not done:
                task.cancel()
