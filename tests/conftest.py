"""A local stand-in for the NevoFlux account service (better-auth device grant).

Mirrors the endpoints and status codes in the handoff's §3.2: errors from the
token poll are HTTP 400 with {"error": ...}; an approval sets a session cookie
the client must never send back.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

DEAD_URL = "http://127.0.0.1:9"
GRANT = "urn:ietf:params:oauth:grant-type:device_code"
POLL_ERRORS = {"pending": "authorization_pending", "slow_down": "slow_down",
               "expired": "expired_token", "denied": "access_denied"}


class AccountDouble:
    def __init__(self):
        self.polls: list[str] = []  # scripted: "pending", "slow_down", "approve", "expired", "denied"
        self.valid: set[str] = set()
        self.revoke_works = True
        self.fail_with: int | None = None
        self.requests: list[dict] = []
        self._issued = 0
        self._server = None
        self.url = ""

    def start(self):
        double = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, obj, cookie=False):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if cookie:
                    self.send_header("Set-Cookie", "better-auth.session_token=s; Path=/; HttpOnly")
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                double.requests.append({"method": method, "path": self.path,
                                        "headers": dict(self.headers), "body": body})
                if double.fail_with:
                    return self._reply(double.fail_with, {})
                bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                route = (method, self.path)
                if route == ("POST", "/api/auth/device/code"):
                    if body.get("client_id") != "nevoflux-muse":
                        return self._reply(400, {"error": "invalid_client"})
                    double._issued += 1
                    return self._reply(200, {
                        "device_code": f"dev-{double._issued}", "user_code": "ABCD-EFGH",
                        "verification_uri": f"{double.url}/device",
                        "verification_uri_complete": f"{double.url}/device?user_code=ABCD-EFGH",
                        "expires_in": 1800, "interval": 5})
                if route == ("POST", "/api/auth/device/token"):
                    if body.get("grant_type") != GRANT or body.get("client_id") != "nevoflux-muse":
                        return self._reply(400, {"error": "invalid_request"})
                    outcome = double.polls.pop(0) if double.polls else "pending"
                    if outcome == "approve":
                        token = f"tok-{len(double.valid) + 1}"
                        double.valid.add(token)
                        return self._reply(200, {"access_token": token, "token_type": "Bearer",
                                                 "expires_in": 604800, "scope": "openid"},
                                           cookie=True)
                    return self._reply(400, {"error": POLL_ERRORS[outcome]})
                if route == ("GET", "/api/auth/token"):
                    if bearer in double.valid:
                        return self._reply(200, {"token": f"jwt-for-{bearer}"})
                    return self._reply(401, {})
                if route == ("POST", "/api/auth/revoke-session"):
                    if bearer not in double.valid:
                        return self._reply(401, {})
                    if double.revoke_works:
                        double.valid.discard(body.get("token"))
                    return self._reply(200, {"status": True})
                return self._reply(404, {})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def paths(self):
        return [r["path"] for r in self.requests]


@pytest.fixture
def account(monkeypatch):
    double = AccountDouble().start()
    monkeypatch.setenv("NEVOFLUX_ACCOUNT_URL", double.url)
    yield double
    double.stop()
