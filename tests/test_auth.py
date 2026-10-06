import asyncio
import re
import socket
import threading

import pytest
from conftest import DEAD_URL

from nevoflux_muse import auth, state

T0 = 1_000_000.0


@pytest.fixture
def clock(monkeypatch):
    t = {"now": T0}
    monkeypatch.setattr(auth, "now", lambda: t["now"])
    return t


def test_account_url(monkeypatch):
    monkeypatch.delenv("NEVOFLUX_ACCOUNT_URL", raising=False)
    assert auth.account_url() == "https://nevoflux.app"
    monkeypatch.setenv("NEVOFLUX_ACCOUNT_URL", "http://x:1/")
    assert auth.account_url() == "http://x:1"


def test_device_code(account):
    dc = auth.AccountClient(account.url).device_code()
    assert dc.user_code == "ABCD-EFGH" and dc.interval == 5 and dc.expires_in == 1800
    assert account.requests[0]["body"]["client_id"] == "nevoflux-muse"


def test_poll_results(account):
    client = auth.AccountClient(account.url)
    account.polls = ["pending", "slow_down", "expired", "denied", "approve"]
    assert client.poll("dev-1") == auth.Pending()
    assert client.poll("dev-1") == auth.SlowDown()
    assert client.poll("dev-1") == auth.Failed("expired_token")
    assert client.poll("dev-1") == auth.Failed("access_denied")
    assert client.poll("dev-1") == auth.Approved("tok-1")


def test_cookies_are_never_sent_back(account):
    client = auth.AccountClient(account.url)
    account.polls = ["approve"]
    token = client.poll("dev-1").token
    client.mint_jwt(token)
    client.revoke(token)
    assert all("Cookie" not in r["headers"] for r in account.requests)


def test_mint_and_revoke(account):
    client = auth.AccountClient(account.url)
    account.valid.add("tok-1")
    assert client.mint_jwt("tok-1") == "jwt-for-tok-1"
    client.revoke("tok-1")
    with pytest.raises(auth.AuthRevoked):
        client.mint_jwt("tok-1")
    with pytest.raises(auth.AuthRevoked):
        client.revoke("tok-1")


def test_unreachable_and_server_errors(account):
    with pytest.raises(auth.AccountUnreachable) as e:
        auth.AccountClient(DEAD_URL).mint_jwt("secret-token")
    assert "secret-token" not in str(e.value)
    account.fail_with = 503
    with pytest.raises(auth.AccountUnreachable):
        auth.AccountClient(account.url).poll("dev-1")


def test_begin_writes_pending(tmp_path, account, clock):
    auth.begin(tmp_path, auth.AccountClient(account.url))
    pending = state.read_state(tmp_path, state.AUTH_PENDING_FILE, auth.PENDING_FIELDS)
    assert pending["device_code"] == "dev-1"
    assert pending["base_url"] == account.url
    assert pending["next_poll_at"] == T0 + 5 and pending["expires_at"] == T0 + 1800


def test_poll_if_due_without_pending(tmp_path):
    assert auth.poll_if_due(tmp_path) is None


def test_poll_if_due_paces_itself(tmp_path, account, clock):
    auth.begin(tmp_path, auth.AccountClient(account.url))
    account.polls = ["pending", "slow_down", "approve"]
    polls = lambda: account.paths().count("/api/auth/device/token")
    for t, expected_polls in [(1, 0), (5, 1), (9, 1), (10, 2), (19, 2), (20, 3)]:
        clock["now"] = T0 + t
        outcome = auth.poll_if_due(tmp_path)
        assert polls() == expected_polls, t
    assert outcome == auth.PollOutcome("approved")
    assert state.read_state(tmp_path, state.ACCOUNT_FILE) == {
        "base_url": account.url, "access_token": "tok-1", "v": 1}
    assert not (tmp_path / state.AUTH_PENDING_FILE).exists()


def test_poll_if_due_failure_drops_pending(tmp_path, account, clock):
    auth.begin(tmp_path, auth.AccountClient(account.url))
    account.polls = ["denied"]
    clock["now"] = T0 + 5
    assert auth.poll_if_due(tmp_path) == auth.PollOutcome("failed", error="access_denied")
    assert not (tmp_path / state.AUTH_PENDING_FILE).exists()


def test_poll_if_due_expires_locally(tmp_path, account, clock):
    auth.begin(tmp_path, auth.AccountClient(account.url))
    clock["now"] = T0 + 1800
    assert auth.poll_if_due(tmp_path) == auth.PollOutcome("failed", error="expired_token")
    assert "/api/auth/device/token" not in account.paths()


def test_poll_if_due_offline_keeps_pending(tmp_path, clock):
    state.write_secret(tmp_path, state.AUTH_PENDING_FILE, {
        "base_url": DEAD_URL, "device_code": "d", "user_code": "U", "verification_uri": "v",
        "verification_uri_complete": "vc", "interval": 5, "expires_at": T0 + 100,
        "next_poll_at": T0})
    outcome = auth.poll_if_due(tmp_path)
    assert outcome.status == "pending" and outcome.poll_error
    assert (tmp_path / state.AUTH_PENDING_FILE).exists()


def account_file(d, base_url, token="tok-1"):
    state.write_secret(d, state.ACCOUNT_FILE, {"base_url": base_url, "access_token": token})


def test_revoke_and_verify(tmp_path, account):
    account.valid.add("tok-1")
    account_file(tmp_path, account.url)
    assert auth.revoke_and_verify(tmp_path) is True
    assert account.paths() == ["/api/auth/revoke-session", "/api/auth/token"]
    assert account.valid == set()


def test_revoke_and_verify_without_a_token(tmp_path):
    assert auth.revoke_and_verify(tmp_path) is False


def test_revoke_and_verify_dead_token(tmp_path, account):
    account_file(tmp_path, account.url)
    assert auth.revoke_and_verify(tmp_path) is True


def test_revoke_that_does_not_stick(tmp_path, account):
    account.valid.add("tok-1")
    account.revoke_works = False
    account_file(tmp_path, account.url)
    with pytest.raises(auth.AccountError):
        auth.revoke_and_verify(tmp_path)


def test_revoke_offline(tmp_path):
    account_file(tmp_path, DEAD_URL)
    with pytest.raises(auth.AccountUnreachable):
        auth.revoke_and_verify(tmp_path)


def test_token_provider(tmp_path, account):
    account.valid.add("tok-1")
    account_file(tmp_path, account.url)
    assert asyncio.run(auth.token_provider(tmp_path)()) == "jwt-for-tok-1"


def test_token_provider_without_account(tmp_path):
    with pytest.raises(auth.AuthRevoked):
        asyncio.run(auth.token_provider(tmp_path)())


def test_token_provider_with_a_bad_base_url(tmp_path):
    account_file(tmp_path, "not a url")
    with pytest.raises(state.UnsupportedState) as e:
        asyncio.run(auth.token_provider(tmp_path)())
    assert state.ACCOUNT_FILE in str(e.value)


@pytest.mark.parametrize("url", ["not a url", "ftp://x", "", "//host"])
def test_account_client_needs_an_http_url(url):
    with pytest.raises(auth.AccountError):
        auth.AccountClient(url)


def test_poll_if_due_with_a_bad_base_url(tmp_path, clock):
    state.write_secret(tmp_path, state.AUTH_PENDING_FILE, {
        "base_url": "ftp://x", "device_code": "d", "user_code": "U", "verification_uri": "v",
        "verification_uri_complete": "vc", "interval": 5, "expires_at": T0 + 100,
        "next_poll_at": T0})
    with pytest.raises(state.UnsupportedState) as e:
        auth.poll_if_due(tmp_path)
    assert state.AUTH_PENDING_FILE in str(e.value)


@pytest.mark.parametrize("reply", [b"garbage\r\n\r\n",
                                   b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort"])
def test_a_malformed_reply_is_unreachable(reply):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        with conn:
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            head, _, body = data.partition(b"\r\n\r\n")
            length = int(re.search(rb"content-length: *(\d+)", head, re.IGNORECASE).group(1))
            while len(body) < length:
                body += conn.recv(4096)
            conn.sendall(reply)
            conn.shutdown(socket.SHUT_WR)
            while conn.recv(4096):
                pass

    threading.Thread(target=serve, daemon=True).start()
    try:
        with pytest.raises(auth.AccountUnreachable) as e:
            auth.AccountClient(f"http://127.0.0.1:{srv.getsockname()[1]}").device_code()
        assert "broken reply" in str(e.value) and "secret" not in str(e.value)
    finally:
        srv.close()
