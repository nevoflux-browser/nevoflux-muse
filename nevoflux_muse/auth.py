"""The NevoFlux account: device grant, relay JWTs, revocation.

The agent never sees a password. `nf auth begin` asks the account service for a
device code and returns at once; the person approves it in a browser while the
agent keeps calling `nf status`, each call polling at most once per interval
(poll_if_due). The access token that comes back is a full account session
until M4 narrows it, so `nf reset` revokes it on the server before deleting it
locally, and keeps it when it cannot prove the revocation.

No cookie jar: the token endpoint sets a session cookie, and sending it back
makes better-auth demand an Origin header (403 MISSING_OR_NULL_ORIGIN).
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from . import state

DEFAULT_ACCOUNT_URL = "https://nevoflux.app"
CLIENT_ID = "nevoflux-muse"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
TIMEOUT = 10
ACCOUNT_FIELDS = {"base_url": str, "access_token": str}
PENDING_FIELDS = {"base_url": str, "device_code": str, "user_code": str, "verification_uri": str,
                  "verification_uri_complete": str, "interval": state.NUMBER,
                  "expires_at": state.NUMBER, "next_poll_at": state.NUMBER}


class AccountError(Exception):
    pass


class AccountUnreachable(AccountError):
    pass


class AuthRevoked(AccountError):
    pass


def now() -> float:
    return time.time()


def account_url() -> str:
    return (os.environ.get("NEVOFLUX_ACCOUNT_URL") or DEFAULT_ACCOUNT_URL).rstrip("/")


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class Approved:
    token: str


@dataclass(frozen=True)
class Pending:
    pass


@dataclass(frozen=True)
class SlowDown:
    pass


@dataclass(frozen=True)
class Failed:
    error: str


class AccountClient:
    def __init__(self, base_url: str):
        url = urllib.parse.urlparse(base_url)
        if url.scheme not in ("http", "https") or not url.hostname:
            raise AccountError("the account service URL must be an http(s):// URL")
        self.base_url = base_url.rstrip("/")

    def _request(self, method: str, path: str, body: dict | None = None,
                 token: str | None = None) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise AccountUnreachable(f"cannot reach {self.base_url}: {reason}") from None
        except http.client.HTTPException as e:  # a malformed status line, a truncated body
            raise AccountUnreachable(
                f"{self.base_url} sent a broken reply ({type(e).__name__})") from None
        if status >= 500:
            raise AccountUnreachable(f"{self.base_url} answered HTTP {status}")
        try:
            obj = json.loads(raw) if raw else {}
        except ValueError:
            obj = {}
        return status, obj if isinstance(obj, dict) else {}

    def device_code(self) -> DeviceCode:
        status, obj = self._request("POST", "/api/auth/device/code",
                                    {"client_id": CLIENT_ID, "scope": "openid profile email"})
        if status != 200:
            raise AccountError(obj.get("error_description") or obj.get("error")
                               or f"device code request answered HTTP {status}")
        try:
            return DeviceCode(
                device_code=str(obj["device_code"]),
                user_code=str(obj["user_code"]),
                verification_uri=str(obj["verification_uri"]),
                verification_uri_complete=str(obj.get("verification_uri_complete")
                                              or obj["verification_uri"]),
                expires_in=int(obj["expires_in"]),
                interval=int(obj.get("interval", 5)),
            )
        except (KeyError, TypeError, ValueError):
            raise AccountError("the account service sent a malformed device code") from None

    def poll(self, device_code: str) -> Approved | Pending | SlowDown | Failed:
        status, obj = self._request("POST", "/api/auth/device/token", {
            "grant_type": GRANT_TYPE, "device_code": device_code, "client_id": CLIENT_ID})
        token = obj.get("access_token")
        if status == 200 and isinstance(token, str) and token:
            return Approved(token)
        error = obj.get("error")
        if error == "authorization_pending":
            return Pending()
        if error == "slow_down":
            return SlowDown()
        return Failed(str(error or f"HTTP {status}"))

    def mint_jwt(self, token: str) -> str:
        status, obj = self._request("GET", "/api/auth/token", token=token)
        if status == 401:
            raise AuthRevoked("the account token is no longer valid; run `nf auth begin --reset`")
        jwt = obj.get("token")
        if status == 200 and isinstance(jwt, str) and jwt:
            return jwt
        raise AccountError(f"the token endpoint answered HTTP {status}")

    def revoke(self, token: str) -> None:
        status, _ = self._request("POST", "/api/auth/revoke-session", {"token": token}, token=token)
        if status == 401:
            raise AuthRevoked("the account token is no longer valid")
        if status != 200:
            raise AccountError(f"revoke-session answered HTTP {status}")


def begin(d: Path, client: AccountClient) -> DeviceCode:
    dc = client.device_code()
    t = now()
    state.write_secret(d, state.AUTH_PENDING_FILE, {
        "base_url": client.base_url,
        "device_code": dc.device_code,
        "user_code": dc.user_code,
        "verification_uri": dc.verification_uri,
        "verification_uri_complete": dc.verification_uri_complete,
        "interval": dc.interval,
        "expires_at": t + dc.expires_in,
        "next_poll_at": t + dc.interval,
    })
    return dc


@dataclass(frozen=True)
class PollOutcome:
    status: str  # "pending" | "approved" | "failed"
    error: str | None = None
    poll_error: str | None = None


def _drop_pending(d: Path) -> None:
    (d / state.AUTH_PENDING_FILE).unlink(missing_ok=True)


def poll_if_due(d: Path) -> PollOutcome | None:
    """Poll the pending device grant once if its interval has passed."""
    pending = state.read_state(d, state.AUTH_PENDING_FILE, PENDING_FIELDS)
    if pending is None:
        return None
    t = now()
    if t >= pending["expires_at"]:
        _drop_pending(d)
        return PollOutcome("failed", error="expired_token")
    if t < pending["next_poll_at"]:
        return PollOutcome("pending")
    try:
        result = AccountClient(pending["base_url"]).poll(pending["device_code"])
    except AccountUnreachable as e:
        return PollOutcome("pending", poll_error=str(e))
    if isinstance(result, Approved):
        state.write_secret(d, state.ACCOUNT_FILE,
                           {"base_url": pending["base_url"], "access_token": result.token})
        _drop_pending(d)
        return PollOutcome("approved")
    if isinstance(result, Failed):
        _drop_pending(d)
        return PollOutcome("failed", error=result.error)
    interval = pending["interval"] + (5 if isinstance(result, SlowDown) else 0)
    state.write_secret(d, state.AUTH_PENDING_FILE,
                       {**pending, "interval": interval, "next_poll_at": t + interval})
    return PollOutcome("pending")


def revoke_and_verify(d: Path) -> bool:
    """Kill the account token on the server. True if there was one; raises if it may live on."""
    account = state.read_state(d, state.ACCOUNT_FILE, ACCOUNT_FIELDS)
    if account is None:
        return False
    client = AccountClient(account["base_url"])
    try:
        client.revoke(account["access_token"])
    except AuthRevoked:
        return True
    try:
        client.mint_jwt(account["access_token"])
    except AuthRevoked:
        return True
    raise AccountError("the account token still works after revoke-session")


def token_provider(d: Path) -> Callable[[], Awaitable[str]]:
    """A fresh relay JWT (15 minutes) for each dial."""

    async def provide() -> str:
        account = state.read_state(d, state.ACCOUNT_FILE, ACCOUNT_FIELDS)
        if account is None:
            raise AuthRevoked("not authorized; run `nf auth begin`")
        client = AccountClient(account["base_url"])
        return await asyncio.to_thread(client.mint_jwt, account["access_token"])

    return provide
