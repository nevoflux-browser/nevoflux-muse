import base64
import io
import json
from pathlib import Path

import pytest
from conftest import DEAD_URL

import nevoflux_muse.state as st
from nevoflux_muse import PROTOCOL_MAX, PROTOCOL_MIN, __version__, auth
from nevoflux_muse.cli import main
from nevoflux_muse.state import ACCOUNT_FILE, PAIRING_FILE


def test_version_for_people(capsys):
    assert main(["version"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == f"nevoflux-muse {__version__} (protocol {PROTOCOL_MIN}-{PROTOCOL_MAX})"


def test_version_for_agents(capsys):
    assert main(["version", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {"client": __version__, "protocol": {"min": PROTOCOL_MIN, "max": PROTOCOL_MAX}}


def test_no_subcommand_is_a_usage_error(capsys):
    assert main([]) == 2
    assert "usage" in capsys.readouterr().err.lower()


@pytest.fixture(autouse=True)
def no_bridge(monkeypatch):
    from nevoflux_muse import daemon
    monkeypatch.setattr(daemon, "SUPPORTED", False)


@pytest.fixture
def home(monkeypatch, tmp_path):
    d = tmp_path / "nf"
    monkeypatch.setenv("NF_HOME", str(d))
    return d


def _populate(d):
    d.mkdir()
    (d / PAIRING_FILE).write_text("{}")
    (d / ACCOUNT_FILE).write_text("{}")


def test_unpair_removes_only_the_pairing(home, capsys):
    _populate(home)
    assert main(["unpair", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"unpaired": True, "state_dir": str(home)}
    assert not (home / PAIRING_FILE).exists()
    assert (home / ACCOUNT_FILE).exists()


def test_unpair_when_not_paired_is_a_no_op(home, capsys):
    assert main(["unpair", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"unpaired": False, "state_dir": str(home)}


def test_unpair_for_people_mentions_the_browser(home, capsys):
    _populate(home)
    assert main(["unpair"]) == 0
    assert "NevoFlux" in capsys.readouterr().out


def test_reset_removes_everything_it_knows(home, capsys):
    _populate(home)
    assert main(["reset", "--local-only", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "revoked": None, "removed": [ACCOUNT_FILE, PAIRING_FILE], "left": [], "state_dir": str(home)}
    assert not home.exists()


def test_reset_with_nothing_there(home, capsys):
    assert main(["reset", "--local-only", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "revoked": None, "removed": [], "left": [], "state_dir": str(home)}


def test_reset_leaves_files_it_does_not_know(home, capsys):
    _populate(home)
    (home / "notes.txt").write_text("mine")
    assert main(["reset", "--local-only", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["revoked"] is None
    assert data["removed"] == [ACCOUNT_FILE, PAIRING_FILE]
    assert data["left"] == ["notes.txt"]
    assert (home / "notes.txt").read_text() == "mine"


def test_reset_failure_is_reported(home, capsys, monkeypatch):
    _populate(home)

    def refuse(self, missing_ok=False):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "unlink", refuse)
    assert main(["reset", "--local-only", "--json"]) == 1
    assert "denied" in json.loads(capsys.readouterr().out)["error"]


PAIR_BLOCK = (
    "NEVOFLUX_AGENT_PAIRING\n"
    "relay: wss://relay.nevoflux.app\n"
    "channel: 2f1c4a90-7b3e-4d1a-9c58-0e6a2b7d4f31\n"
    "code: A-BCDE-FGHJ-KMNP\n"
)


def test_pair_from_stdin(home, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(PAIR_BLOCK))
    assert main(["pair", "--from-stdin", "--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {
        "paired": True,
        "relay": "wss://relay.nevoflux.app",
        "channel": "2f1c4a90-7b3e-4d1a-9c58-0e6a2b7d4f31"
    }
    assert "BCDE" not in out
    assert (home / PAIRING_FILE).exists()


def test_pair_rejects_a_bad_block(home, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin",
                        io.StringIO(PAIR_BLOCK.replace("KMNP", "KMN")))
    assert main(["pair", "--from-stdin", "--json"]) == 1
    assert "error" in json.loads(capsys.readouterr().out)
    assert not home.exists()


def test_pair_requires_from_stdin(home, capsys):
    with pytest.raises(SystemExit) as e:
        main(["pair"])
    assert e.value.code == 2


T0 = 1_000_000.0


@pytest.fixture
def clock(monkeypatch):
    t = {"now": T0}
    monkeypatch.setattr(auth, "now", lambda: t["now"])
    return t


def run_json(capsys, *argv):
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_auth_begin(home, account, capsys, clock):
    code, out = run_json(capsys, "auth", "begin")
    assert code == 0
    assert out == {"verification_uri": f"{account.url}/device",
                   "verification_uri_complete": f"{account.url}/device?user_code=ABCD-EFGH",
                   "user_code": "ABCD-EFGH", "expires_in": 1800}
    assert (home / st.AUTH_PENDING_FILE).exists()


def test_auth_begin_when_authorized(home, account, capsys):
    st.write_secret(home, ACCOUNT_FILE, {"base_url": account.url, "access_token": "tok-1"})
    assert run_json(capsys, "auth", "begin") == (0, {"already_authorized": True})
    assert account.requests == []


def test_auth_begin_reset_revokes_first(home, account, capsys, clock):
    account.valid.add("tok-1")
    st.write_secret(home, ACCOUNT_FILE, {"base_url": account.url, "access_token": "tok-1"})
    code, out = run_json(capsys, "auth", "begin", "--reset")
    assert code == 0 and out["user_code"] == "ABCD-EFGH"
    assert account.paths()[:2] == ["/api/auth/revoke-session", "/api/auth/token"]
    assert not (home / ACCOUNT_FILE).exists()


def test_auth_begin_reset_offline_keeps_token(home, account, capsys):
    st.write_secret(home, ACCOUNT_FILE, {"base_url": DEAD_URL, "access_token": "tok-1"})
    code, out = run_json(capsys, "auth", "begin", "--reset")
    assert code == 1 and "error" in out
    assert (home / ACCOUNT_FILE).exists()


def test_auth_alone_is_a_usage_error(home, capsys):
    assert main(["auth"]) == 2


def test_status_flow(home, account, capsys, clock, monkeypatch):
    assert run_json(capsys, "status")[1] == {"state": "not_paired", "paired": False, "auth": "none"}
    run_json(capsys, "auth", "begin")
    clock["now"] = T0 + 1
    code, out = run_json(capsys, "status")
    assert code == 0
    assert out == {"state": "awaiting_device_approval", "paired": False, "auth": "pending",
                   "user_code": "ABCD-EFGH", "verification_uri": f"{account.url}/device",
                   "verification_uri_complete": f"{account.url}/device?user_code=ABCD-EFGH",
                   "expires_in": 1799}
    account.polls = ["approve"]
    clock["now"] = T0 + 5
    assert run_json(capsys, "status")[1] == {"state": "not_paired", "paired": False, "auth": "ok"}
    monkeypatch.setattr("sys.stdin", io.StringIO(PAIR_BLOCK))
    run_json(capsys, "pair", "--from-stdin")
    assert run_json(capsys, "status")[1] == {"state": "ready", "paired": True, "auth": "ok",
                                             "bridge": "unsupported on Windows"}
    everything = json.dumps(account.requests)
    assert "Cookie" not in everything


def test_status_polls_at_most_once_per_interval(home, account, capsys, clock):
    run_json(capsys, "auth", "begin")
    account.polls = ["pending"] * 20
    for second in range(1, 21):
        clock["now"] = T0 + second
        run_json(capsys, "status")
    assert account.paths().count("/api/auth/device/token") == 4  # at 5, 10, 15, 20


def test_status_reports_a_denial_once(home, account, capsys, clock):
    run_json(capsys, "auth", "begin")
    account.polls = ["denied"]
    clock["now"] = T0 + 5
    assert run_json(capsys, "status")[1] == {"state": "not_paired", "paired": False,
                                             "auth": "none", "last_error": "access_denied"}
    assert "last_error" not in run_json(capsys, "status")[1]


def test_status_offline_while_pending(home, capsys, clock):
    st.write_secret(home, st.AUTH_PENDING_FILE, {
        "base_url": DEAD_URL, "device_code": "d", "user_code": "U", "verification_uri": "v",
        "verification_uri_complete": "vc", "interval": 5, "expires_at": T0 + 100,
        "next_poll_at": T0})
    code, out = run_json(capsys, "status")
    assert code == 0
    assert out["state"] == "awaiting_device_approval" and "poll_error" in out


def test_status_with_a_corrupt_file(home, capsys):
    home.mkdir()
    (home / PAIRING_FILE).write_text("{trunc")
    code, out = run_json(capsys, "status")
    assert code == 1
    assert PAIRING_FILE in out["error"] and "nf reset --local-only" in out["error"]


def test_status_never_prints_secrets(home, account, capsys, clock):
    account.valid.add("tok-secret")
    st.write_secret(home, ACCOUNT_FILE, {"base_url": account.url, "access_token": "tok-secret"})
    run_json(capsys, "auth", "begin", "--reset")
    clock["now"] = T0 + 1
    assert main(["status"]) == 0
    printed = capsys.readouterr().out
    assert "tok-secret" not in printed and "dev-1" not in printed


def write_account(home, base_url, token="tok-1"):
    st.write_secret(home, ACCOUNT_FILE, {"base_url": base_url, "access_token": token})
    st.write_secret(home, PAIRING_FILE, {"relay": "wss://r", "channel": "c", "key": "AA=="})


def test_reset_revokes_then_deletes(home, account, capsys):
    account.valid.add("tok-1")
    write_account(home, account.url)
    code, out = run_json(capsys, "reset")
    assert code == 0
    assert out == {"revoked": True, "removed": [ACCOUNT_FILE, PAIRING_FILE], "left": [],
                   "state_dir": str(home)}
    assert account.valid == set()
    assert not home.exists()


def test_reset_keeps_token_when_offline_then_finishes(home, account, capsys):
    account.valid.add("tok-1")
    write_account(home, DEAD_URL)
    code, out = run_json(capsys, "reset")
    assert code == 1
    assert out["revoked"] is False and "error" in out
    assert out["removed"] == [PAIRING_FILE] and out["left"] == [ACCOUNT_FILE]
    assert (home / ACCOUNT_FILE).exists()
    # Back online: the same command finishes the job.
    st.write_secret(home, ACCOUNT_FILE, {"base_url": account.url, "access_token": "tok-1"})
    code, out = run_json(capsys, "reset")
    assert code == 0 and out["revoked"] is True and not home.exists()


def test_reset_when_revoke_does_not_stick(home, account, capsys):
    account.valid.add("tok-1")
    account.revoke_works = False
    write_account(home, account.url)
    code, out = run_json(capsys, "reset")
    assert code == 1 and out["revoked"] is False
    assert (home / ACCOUNT_FILE).exists()


def test_reset_with_an_already_dead_token(home, account, capsys):
    write_account(home, account.url)
    code, out = run_json(capsys, "reset")
    assert code == 0 and out["revoked"] is True


def test_reset_local_only_skips_the_server(home, account, capsys):
    account.valid.add("tok-1")
    write_account(home, account.url)
    code, out = run_json(capsys, "reset", "--local-only")
    assert code == 0 and out["revoked"] is None
    assert account.requests == [] and not home.exists()


def test_reset_with_a_corrupt_account_file(home, capsys):
    home.mkdir()
    (home / ACCOUNT_FILE).write_text("garbage")
    code, out = run_json(capsys, "reset")
    assert code == 1 and out["revoked"] is False
    assert "nf reset --local-only" in out["error"]
    assert (home / ACCOUNT_FILE).exists()


def test_status_never_prints_secrets_as_json(home, account, capsys, clock):
    account.valid.add("tok-secret")
    st.write_secret(home, ACCOUNT_FILE, {"base_url": account.url, "access_token": "tok-secret"})
    run_json(capsys, "auth", "begin", "--reset")
    clock["now"] = T0 + 1
    assert main(["status", "--json"]) == 0
    printed = capsys.readouterr().out
    assert "tok-secret" not in printed and "dev-1" not in printed


def test_status_text_shows_the_last_error(home, account, capsys, clock):
    run_json(capsys, "auth", "begin")
    st.write_secret(home, PAIRING_FILE, {"relay": "wss://r", "channel": "c", "key": "AA=="})
    account.polls = ["denied"]
    clock["now"] = T0 + 5
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Not signed in" in out and "access_denied" in out


def test_status_text_shows_the_poll_error(home, capsys, clock):
    st.write_secret(home, st.AUTH_PENDING_FILE, {
        "base_url": DEAD_URL, "device_code": "d", "user_code": "U", "verification_uri": "v",
        "verification_uri_complete": "vc", "interval": 5, "expires_at": T0 + 100,
        "next_poll_at": T0})
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Waiting for approval" in out and "cannot reach" in out


def test_status_with_a_mistyped_pending_file(home, capsys):
    st.write_secret(home, st.AUTH_PENDING_FILE, {
        "base_url": "http://x", "device_code": "d", "user_code": "U", "verification_uri": "v",
        "verification_uri_complete": "vc", "interval": 5, "expires_at": "soon",
        "next_poll_at": 0})
    code, out = run_json(capsys, "status")
    assert code == 1
    assert st.AUTH_PENDING_FILE in out["error"] and "nf reset --local-only" in out["error"]


@pytest.mark.parametrize("base_url", [5, "not a url"])
def test_reset_with_a_bad_base_url_keeps_the_account(home, capsys, base_url):
    st.write_secret(home, ACCOUNT_FILE, {"base_url": base_url, "access_token": "tok-1"})
    code, out = run_json(capsys, "reset")
    assert code == 1 and out["revoked"] is False and "error" in out
    assert (home / ACCOUNT_FILE).exists()


def test_reset_survives_an_os_error(home, capsys, monkeypatch):
    write_account(home, "http://x")

    def boom(d):
        raise PermissionError("secret-xyz denied")

    monkeypatch.setattr(auth, "revoke_and_verify", boom)
    code, out = run_json(capsys, "reset")
    assert code == 1 and out["revoked"] is False
    assert (home / ACCOUNT_FILE).exists()


@pytest.mark.parametrize("flags", [["--json"], []])
def test_an_unexpected_error_prints_only_its_type(home, capsys, monkeypatch, flags):
    def boom(d):
        raise RuntimeError("secret-xyz")

    monkeypatch.setattr(auth, "poll_if_due", boom)
    assert main(["status", *flags]) == 1
    cap = capsys.readouterr()
    assert "secret-xyz" not in cap.out + cap.err
    if flags:
        assert json.loads(cap.out) == {"error": "unexpected RuntimeError"}
    else:
        assert "nf: unexpected RuntimeError" in cap.err


def test_status_with_a_bad_pending_url(home, capsys, clock):
    st.write_secret(home, st.AUTH_PENDING_FILE, {
        "base_url": "ftp://x", "device_code": "d", "user_code": "U", "verification_uri": "v",
        "verification_uri_complete": "vc", "interval": 5, "expires_at": T0 + 100,
        "next_poll_at": T0})
    code, out = run_json(capsys, "status")
    assert code == 1 and st.AUTH_PENDING_FILE in out["error"]


class FakeBridge:
    """Stands in for daemon.ensure/stop and ipc.request."""

    def __init__(self, monkeypatch, replies=None, ensure_error=None, stop_error=None):
        import contextlib

        from nevoflux_muse import daemon, ipc
        self.sent, self.stopped, self.ensured = [], 0, 0
        self.replies = replies or {}
        monkeypatch.setattr(daemon, "SUPPORTED", True)

        def ensure(d):
            self.ensured += 1
            if ensure_error:
                raise ensure_error
            return False

        def request(d, msg, timeout):
            self.sent.append(msg)
            reply = self.replies.get(msg["op"], {"ok": True})
            if isinstance(reply, Exception):
                raise reply
            return reply

        def stop(d, timeout=5.0):
            self.stopped += 1
            if stop_error:
                raise stop_error
            return True

        @contextlib.contextmanager
        def held(d, timeout=10.0):
            stop(d)
            yield

        monkeypatch.setattr(daemon, "ensure", ensure)
        monkeypatch.setattr(daemon, "stop", stop)
        monkeypatch.setattr(daemon, "held", held, raising=False)
        monkeypatch.setattr(daemon, "alive", lambda d: {"ok": True, "pid": 42})
        monkeypatch.setattr(ipc, "request", request)


PNG = base64.b64encode(b"\x89PNG" + bytes(96)).decode()
RESULT = {"content": [{"type": "text", "text": "hello"},
                      {"type": "image", "data": PNG, "mimeType": "image/png"}],
          "isError": False}


def test_call_args():
    from nevoflux_muse.cli import _call_args
    assert _call_args(["url=https://a.b/c?x=1", "n=3", "on=true", "s=\"3\"", "o={\"a\":1}"],
                      False) == {"url": "https://a.b/c?x=1", "n": 3, "on": True, "s": "3",
                                 "o": {"a": 1}}


@pytest.mark.parametrize("pairs", [["noequals"], ["=v"], ["k=1", "k=2"]])
def test_call_args_usage_errors(pairs):
    from nevoflux_muse.cli import _call_args, _Usage
    with pytest.raises(_Usage):
        _call_args(pairs, False)


def test_call_args_from_stdin(monkeypatch):
    from nevoflux_muse.cli import _call_args, _Usage
    monkeypatch.setattr("sys.stdin", io.StringIO('{"url": "https://x"}'))
    assert _call_args([], True) == {"url": "https://x"}
    with pytest.raises(_Usage):
        _call_args(["a=1"], True)
    monkeypatch.setattr("sys.stdin", io.StringIO("[1]"))
    with pytest.raises(_Usage):
        _call_args([], True)


def test_call_text_and_json(home, capsys, monkeypatch):
    fake = FakeBridge(monkeypatch, {"call": {"ok": True, "result": RESULT}})
    assert main(["call", "browser_snapshot", "depth=2"]) == 0
    assert capsys.readouterr().out.splitlines() == ["hello", "[image image/png, 100 bytes]"]
    assert fake.sent == [{"op": "call", "tool": "browser_snapshot", "args": {"depth": 2},
                          "timeout": 120.0}]
    assert main(["call", "browser_snapshot", "--timeout", "5", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == RESULT
    assert fake.sent[-1]["timeout"] == 5.0


def test_call_options_mix_with_arguments(home, capsys, monkeypatch):
    fake = FakeBridge(monkeypatch, {"call": {"ok": True, "result": RESULT}})
    assert main(["call", "x", "a=1", "--timeout", "5", "b=2", "--json"]) == 0
    assert fake.sent == [{"op": "call", "tool": "x", "args": {"a": 1, "b": 2}, "timeout": 5.0}]


def test_call_tool_error_exits_1(home, capsys, monkeypatch):
    FakeBridge(monkeypatch, {"call": {"ok": True, "result": {**RESULT, "isError": True}}})
    assert main(["call", "x"]) == 1


def test_call_bridge_error(home, capsys, monkeypatch):
    FakeBridge(monkeypatch, {"call": {"ok": False, "error": {
        "code": "mcp_error", "message": "bash: not_allowed", "data": {"code": -32602}}}})
    assert main(["call", "bash", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "error": "bash: not_allowed", "code": "mcp_error", "data": {"code": -32602}}
    assert main(["call", "bash"]) == 1
    assert "bash: not_allowed" in capsys.readouterr().err


def test_call_usage_error_exits_2(home, capsys, monkeypatch):
    FakeBridge(monkeypatch)
    assert main(["call", "x", "broken"]) == 2


def test_call_when_the_bridge_cannot_start(home, capsys, monkeypatch):
    from nevoflux_muse import ipc
    FakeBridge(monkeypatch, ensure_error=ipc.BridgeDown("the bridge did not start"))
    assert main(["call", "x", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "the bridge did not start"


def test_tools(home, capsys, monkeypatch):
    tools = [{"name": "browser_snapshot", "description": "Snapshot.\nMore.", "inputSchema": {}},
             {"name": "browser_navigate", "description": None, "inputSchema": {}}]
    FakeBridge(monkeypatch, {"tools": {"ok": True, "tools": tools}})
    assert main(["tools"]) == 0
    assert capsys.readouterr().out.splitlines() == ["browser_snapshot — Snapshot.",
                                                    "browser_navigate"]
    assert main(["tools", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == tools


@pytest.mark.parametrize("argv", [["daemon"], ["ensure"], ["tools"], ["call", "x"]])
def test_bridge_commands_on_windows(home, capsys, argv):
    assert main([*argv, "--json"]) == 1
    assert "unsupported on Windows" in json.loads(capsys.readouterr().out)["error"]


def ready_home(home):
    st.write_secret(home, PAIRING_FILE, {"relay": "wss://r", "channel": "c", "key": "AA=="})
    st.write_secret(home, ACCOUNT_FILE, {"base_url": "http://127.0.0.1:9", "access_token": "t"})


def test_status_asks_the_bridge(home, capsys, monkeypatch):
    ready_home(home)
    FakeBridge(monkeypatch, {"status": {
        "ok": True, "state": "connected", "since": "2026-10-06T00:00:00+00:00", "pid": 42,
        "head": {"name": "nevoflux-head", "version": "0.3.15", "protocol": 1},
        "last_error": None}})
    code, out = run_json(capsys, "status")
    assert code == 0
    assert out == {"state": "connected", "paired": True, "auth": "ok",
                   "bridge": {"pid": 42, "since": "2026-10-06T00:00:00+00:00"},
                   "head": {"name": "nevoflux-head", "version": "0.3.15", "protocol": 1}}


def test_status_when_the_bridge_cannot_start(home, capsys, monkeypatch):
    from nevoflux_muse import ipc
    ready_home(home)
    FakeBridge(monkeypatch, ensure_error=ipc.BridgeDown("the bridge did not start"))
    code, out = run_json(capsys, "status")
    assert code == 0 and out["state"] == "bridge_down"
    assert out["bridge_error"] == "the bridge did not start"


def test_status_text_for_bridge_states(home, capsys, monkeypatch):
    ready_home(home)
    FakeBridge(monkeypatch, {"status": {"ok": True, "state": "head_offline", "since": "s",
                                        "pid": 1, "head": None, "last_error": None}})
    assert main(["status"]) == 0
    assert "not online" in capsys.readouterr().out


def test_pair_and_approval_reload_the_bridge(home, account, capsys, clock, monkeypatch):
    fake = FakeBridge(monkeypatch, {"status": {"ok": True, "state": "connecting", "since": "s",
                                               "pid": 1, "head": None, "last_error": None}})
    monkeypatch.setattr("sys.stdin", io.StringIO(PAIR_BLOCK))
    run_json(capsys, "pair", "--from-stdin")
    assert fake.sent == [{"op": "reload"}]
    run_json(capsys, "auth", "begin")
    account.polls = ["approve"]
    clock["now"] = T0 + 5
    _, out = run_json(capsys, "status")
    assert out["state"] == "connecting"
    assert [m["op"] for m in fake.sent] == ["reload", "reload", "status"]


def test_unpair_and_reset_stop_the_bridge(home, capsys, monkeypatch):
    fake = FakeBridge(monkeypatch)
    _populate(home)
    main(["unpair", "--json"])
    main(["reset", "--local-only", "--json"])
    capsys.readouterr()
    assert fake.stopped == 2


def test_ensure_and_daemon_detach(home, capsys, monkeypatch):
    from nevoflux_muse import daemon
    FakeBridge(monkeypatch)
    assert run_json(capsys, "ensure") == (0, {"running": True, "started": False})
    # daemon refuses when one is already running
    code, out = run_json(capsys, "daemon", "--detach")
    assert code == 1 and "already running" in out["error"]
    monkeypatch.setattr(daemon, "alive", lambda d: None)
    monkeypatch.setattr(daemon, "run_foreground", lambda d: 0)
    assert main(["daemon"]) == 0


def test_unpair_and_reset_keep_everything_when_the_bridge_will_not_stop(home, capsys,
                                                                       monkeypatch):
    from nevoflux_muse import ipc
    FakeBridge(monkeypatch, stop_error=ipc.BridgeDown("the bridge did not stop"))
    _populate(home)
    for argv in (["unpair"], ["reset", "--local-only"]):
        code, out = run_json(capsys, *argv)
        assert code == 1 and out["error"] == "the bridge did not stop"
    assert (home / PAIRING_FILE).exists() and (home / ACCOUNT_FILE).exists()


def test_ensure_with_an_unreadable_pairing(home, capsys, monkeypatch):
    from nevoflux_muse import daemon
    ready_home(home)  # its key is not 32 bytes
    monkeypatch.setattr(daemon, "SUPPORTED", True)
    monkeypatch.setattr(daemon, "alive", lambda d: None)
    monkeypatch.setattr(daemon, "detach", lambda d: pytest.fail("must not start a bridge"))
    code, out = run_json(capsys, "ensure")
    assert code == 1
    assert PAIRING_FILE in out["error"] and "nf reset --local-only" in out["error"]
