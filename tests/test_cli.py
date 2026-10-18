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
from nevoflux_muse.testing.fake_head import (
    APPROVAL_ID,
    JPEG_B64,
    SESSION_ID,
    TASK_ID,
    approval_item,
    task_result,
    task_state,
    tool_error,
)


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
        monkeypatch.setattr(daemon, "held", held)
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
    out = capsys.readouterr().out
    assert ("The browser is not online (NevoFlux is closed, this agent was paused in the "
            "NevoFlux sidebar, or it was removed there).") in out


def test_head_offline_json_state_is_unchanged(home, capsys, monkeypatch):
    ready_home(home)
    FakeBridge(monkeypatch, {"status": {"ok": True, "state": "head_offline", "since": "s",
                                        "pid": 1, "head": None, "last_error": None}})
    code, out = run_json(capsys, "status")
    assert code == 0 and out["state"] == "head_offline"


def test_install_guide_names_the_sidebar_pause():
    guide = (Path(__file__).resolve().parent.parent / "INSTALL.md").read_text(encoding="utf-8")
    row = next(line for line in guide.splitlines() if line.startswith("| `head_offline` |"))
    assert "paused" in row and "Resume" in row
    assert "it reconnects by itself when the browser returns" not in row
    assert "is this agent paused in the NevoFlux sidebar" in guide


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


def test_ensure_not_ready_exits_0(home, capsys, monkeypatch):
    from nevoflux_muse import daemon
    monkeypatch.setattr(daemon, "SUPPORTED", True)

    def not_ready(d):
        raise daemon.NotReady("pair first")

    monkeypatch.setattr(daemon, "ensure", not_ready)
    assert run_json(capsys, "ensure") == (0, {"running": False, "reason": "not_ready"})


def test_ensure_failure_has_a_reason(home, capsys, monkeypatch):
    from nevoflux_muse import daemon, ipc
    monkeypatch.setattr(daemon, "SUPPORTED", True)

    def broken(d):
        raise ipc.BridgeDown("the bridge did not start")

    monkeypatch.setattr(daemon, "ensure", broken)
    assert run_json(capsys, "ensure") == (1, {"running": False, "reason": "failed",
                                             "error": "the bridge did not start"})


def test_detach_without_a_pid_fails(home, capsys, monkeypatch):
    from nevoflux_muse import daemon
    FakeBridge(monkeypatch)
    answers = iter([None, None])  # not running before; still no answer after ensure
    monkeypatch.setattr(daemon, "alive", lambda d: next(answers))
    code, out = run_json(capsys, "daemon", "--detach")
    assert code == 1 and "does not answer" in out["error"]


def test_status_reports_supervision(home, capsys):
    st.write_supervision(home, "detached+watchdog")
    assert run_json(capsys, "status")[1]["supervision"] == "detached+watchdog"


APPROVAL = {"id": "3f9a1c2e", "message": "Allow browser_click?", "expires_in": 54}


def test_a_call_that_needs_approval_exits_3(home, capsys, monkeypatch):
    FakeBridge(monkeypatch, {"call": {"ok": True, "approval_required": APPROVAL}})
    assert main(["call", "browser_click", "id=3", "--json"]) == 3
    assert json.loads(capsys.readouterr().out) == {"approval_required": APPROVAL}
    assert main(["call", "browser_click", "id=3"]) == 3
    out = capsys.readouterr().out
    assert "Allow browser_click?" in out and "54s" in out
    assert "nf approve 3f9a1c2e" in out and "nf deny 3f9a1c2e" in out


def test_approve_prints_the_call_result(home, capsys, monkeypatch):
    fake = FakeBridge(monkeypatch, {"approve": {"ok": True, "result": RESULT}})
    assert main(["approve", "3f9a1c2e", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == RESULT
    assert fake.sent == [{"op": "approve", "id": "3f9a1c2e", "timeout": 120.0}]
    assert main(["approve", "3f9a1c2e", "--timeout", "30"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "hello"
    assert fake.sent[-1]["timeout"] == 30.0


def test_approve_can_meet_another_approval(home, capsys, monkeypatch):
    other = {**APPROVAL, "id": "77aa00bb"}
    FakeBridge(monkeypatch, {"approve": {"ok": True, "approval_required": other}})
    assert main(["approve", "3f9a1c2e", "--json"]) == 3
    assert json.loads(capsys.readouterr().out) == {"approval_required": other}


def test_deny_prints_the_heads_refusal(home, capsys, monkeypatch):
    refusal = {"content": [{"type": "text", "text": "refused"}], "isError": True,
               "structuredContent": {"error": {"code": "approval_denied", "message": "refused",
                                               "reason": "declined"}}}
    fake = FakeBridge(monkeypatch, {"deny": {"ok": True, "result": refusal}})
    assert main(["deny", "3f9a1c2e", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "error": "approval_denied", "message": "refused", "reason": "declined"}
    assert fake.sent == [{"op": "deny", "id": "3f9a1c2e", "timeout": 120.0}]


def test_answers_without_a_running_call(home, capsys, monkeypatch):
    FakeBridge(monkeypatch, {"approve": {"ok": True, "answered": "approved"},
                             "deny": {"ok": True, "answered": "denied"}})
    assert main(["approve", "3f9a1c2e", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"approved": True}
    assert main(["deny", "3f9a1c2e", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "approval_denied"


def test_an_expired_approval(home, capsys, monkeypatch):
    FakeBridge(monkeypatch, {"approve": {"ok": False, "error": {
        "code": "approval_expired", "message": "that approval is no longer open"}}})
    assert main(["approve", "3f9a1c2e", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "error": "approval_expired", "message": "that approval is no longer open"}
    assert main(["approve", "3f9a1c2e"]) == 1
    assert "nf: approval_expired: that approval is no longer open" in capsys.readouterr().err


def test_a_call_lost_with_its_connection_fails(home, capsys, monkeypatch):
    FakeBridge(monkeypatch, {"call": {"ok": False, "error": {
        "code": "connection_lost", "message": "the connection to the browser was lost"}}})
    assert main(["call", "browser_click", "--json"]) == 1
    assert "connection to the browser was lost" in json.loads(capsys.readouterr().out)["error"]


def test_pending(home, capsys, monkeypatch):
    listed = [{"id": "3f9a1c2e", "tool": "browser_click", "message": "Allow browser_click?",
               "expires_in": 40},
              {"id": "0badf00d", "tool": None, "message": "Allow it?", "expires_in": 12}]
    fake = FakeBridge(monkeypatch, {"pending": {"ok": True, "pending": listed}})
    assert main(["pending", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == listed
    assert fake.sent == [{"op": "pending"}]
    assert main(["pending"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "3f9a1c2e  browser_click  40s  Allow browser_click?", "0badf00d  ?  12s  Allow it?"]


def test_pending_joins_a_multi_line_message(home, capsys, monkeypatch):
    message = "Click the button\nTab: Checkout\nWhy: to pay"
    listed = [{"id": "3f9a1c2e", "tool": "browser_click", "message": message, "expires_in": 40}]
    FakeBridge(monkeypatch, {"pending": {"ok": True, "pending": listed}})
    assert main(["pending", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["message"] == message
    assert main(["pending"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "3f9a1c2e  browser_click  40s  Click the button / Tab: Checkout / Why: to pay"]


def test_pending_when_nothing_waits(home, capsys, monkeypatch):
    FakeBridge(monkeypatch, {"pending": {"ok": True, "pending": []}})
    assert main(["pending"]) == 0
    assert capsys.readouterr().out.strip() == "No approvals are waiting."


TOOL_ERRORS = [
    ("unknown_tool", {}),
    ("tier_denied", {"tier": "read-only", "required": "browser-auto"}),
    ("approval_denied", {"reason": "client_cannot_approve"}),
    ("approval_timeout", {}),
    ("browser_unavailable", {}),
    ("no_agent_tab", {}),
    ("tab_closed", {"tab_id": 7}),
    ("tool_failed", {"detail": "element 12 not found"}),
]


@pytest.mark.parametrize(("code", "extra"), TOOL_ERRORS)
def test_call_tool_errors_print_their_code(home, capsys, monkeypatch, code, extra):
    result = {"content": [{"type": "text", "text": "why"}], "isError": True,
              "structuredContent": {"error": {"code": code, "message": "why", **extra}}}
    FakeBridge(monkeypatch, {"call": {"ok": True, "result": result}})
    assert main(["call", "browser_click", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": code, "message": "why", **extra}
    assert main(["call", "browser_click"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and f"nf: {code}: why" in captured.err


@pytest.mark.parametrize("argv", [["approve", "x"], ["deny", "x"], ["pending"]])
def test_approval_commands_on_windows(home, capsys, argv):
    assert main([*argv, "--json"]) == 1
    assert "unsupported on Windows" in json.loads(capsys.readouterr().out)["error"]


def test_approve_needs_an_id(home, capsys):
    with pytest.raises(SystemExit) as e:
        main(["approve"])
    assert e.value.code == 2


STARTED = {"task_id": TASK_ID, "session": SESSION_ID, "state": "running"}
STEPS = [{"id": "t1", "at": "2026-10-16T10:00:01Z", "tool": "browser_navigate",
          "status": "started"},
         {"id": "t1", "at": "2026-10-16T10:00:03Z", "tool": "browser_navigate", "status": "ok"}]


def task_reply(structured, image=None):
    return {"ok": True, "result": task_result(structured, image=image)}


def test_task_start(home, capsys, monkeypatch):
    fake = FakeBridge(monkeypatch, {"call": task_reply(STARTED)})
    monkeypatch.setattr("sys.stdin", io.StringIO("  Make a calculator Canvas\n"))
    assert main(["task", "start", "--prompt-stdin", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == STARTED
    assert fake.sent == [{"op": "call", "tool": "nevoflux_task_start",
                          "args": {"prompt": "Make a calculator Canvas"}, "timeout": 120.0}]
    assert main(["task", "start", "Make it blue", "--session", SESSION_ID]) == 0
    assert fake.sent[-1]["args"] == {"prompt": "Make it blue", "session": SESSION_ID}
    out = capsys.readouterr().out
    assert f"Task {TASK_ID} started in session {SESSION_ID}." in out
    assert f"nf task wait {TASK_ID}" in out


def test_task_start_takes_8000_characters(home, capsys, monkeypatch):
    fake = FakeBridge(monkeypatch, {"call": task_reply(STARTED)})
    monkeypatch.setattr("sys.stdin", io.StringIO("é" * 8000))
    assert main(["task", "start", "--prompt-stdin", "--json"]) == 0
    assert len(fake.sent[0]["args"]["prompt"]) == 8000


@pytest.mark.parametrize(("argv", "stdin"), [
    (["task", "start"], ""),
    (["task", "start", "x", "--prompt-stdin"], "y"),
    (["task", "start", "--prompt-stdin"], "   \n"),
    (["task", "start", "--prompt-stdin"], "a" * 8001),
])
def test_task_start_usage_errors(home, capsys, monkeypatch, argv, stdin):
    fake = FakeBridge(monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert main(argv) == 2
    assert fake.sent == [] and "nf task start:" in capsys.readouterr().err


@pytest.mark.parametrize(("code", "extra"), [
    ("task_busy", {"task_id": TASK_ID}),
    ("task_busy", {"provider": "claude-code"}),
    ("rate_limited", {"retry_after": 120}),
    ("session_not_found", {"session": "s-9"}),
    ("paused", {}),
    ("browser_unavailable", {}),
])
def test_task_start_errors_print_their_code(home, capsys, monkeypatch, code, extra):
    FakeBridge(monkeypatch, {"call": {"ok": True, "result": tool_error(code, "why", **extra)}})
    assert main(["task", "start", "x", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": code, "message": "why", **extra}


def test_an_old_nevoflux_without_delegation(home, capsys, monkeypatch):
    old = tool_error("unknown_tool", "nevoflux_task_start is not available over the agent channel")
    FakeBridge(monkeypatch, {"call": {"ok": True, "result": old}})
    assert main(["task", "start", "x", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "unknown_tool"


def test_task_wait_running(home, capsys, monkeypatch):
    running = task_state("running", steps=STEPS)
    fake = FakeBridge(monkeypatch, {"call": task_reply(running)})
    assert main(["task", "wait", TASK_ID, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == running
    assert fake.sent == [{"op": "call", "tool": "nevoflux_task_wait",
                          "args": {"task_id": TASK_ID, "timeout_secs": 30}, "timeout": 60.0}]
    assert main(["task", "wait", TASK_ID, "--timeout-secs", "60"]) == 0
    assert fake.sent[-1]["args"]["timeout_secs"] == 60 and fake.sent[-1]["timeout"] == 90.0
    assert capsys.readouterr().out.splitlines() == [
        f"Task {TASK_ID}: running",
        "  2026-10-16T10:00:01Z  browser_navigate  started",
        "  2026-10-16T10:00:03Z  browser_navigate  ok"]


@pytest.mark.parametrize("secs", ["0", "61", "1.5", "x"])
def test_task_wait_timeout_bounds(home, capsys, monkeypatch, secs):
    fake = FakeBridge(monkeypatch)
    with pytest.raises(SystemExit) as e:
        main(["task", "wait", TASK_ID, "--timeout-secs", secs])
    assert e.value.code == 2 and fake.sent == []


def test_task_wait_with_approvals_exits_3(home, capsys, monkeypatch):
    waiting = task_state("awaiting_approval", approvals=[
        approval_item("tool"),
        approval_item("question", approval_id="11111111-2222-4333-8444-555555555555"),
        approval_item("plan", approval_id="66666666-7777-4888-8999-aaaaaaaaaaaa")])
    FakeBridge(monkeypatch, {"call": task_reply(waiting)})
    assert main(["task", "wait", TASK_ID, "--json"]) == 3
    assert json.loads(capsys.readouterr().out) == waiting
    assert main(["task", "wait", TASK_ID]) == 3
    out = capsys.readouterr().out
    assert (f"  approval {APPROVAL_ID}  tool  browser_click  1740s  "
            "Submit the form on example.com") in out
    assert "    question: Which colour?" in out and "    options: Blue | Red" in out
    assert "    plan: Render the Canvas" in out
    assert "`nf approve <approval_id>`" in out and "--answer TEXT" in out
    assert "always" not in out.lower()  # allow_always is never offered (C8)


def test_a_desktop_only_approval_is_not_offered(home, capsys, monkeypatch):
    waiting = task_state("awaiting_approval", approvals=[
        approval_item(tool=None, choices=[], desktop_only=True)])
    FakeBridge(monkeypatch, {"call": task_reply(waiting)})
    assert main(["task", "wait", TASK_ID]) == 3
    out = capsys.readouterr().out
    assert "waiting for the person at the desktop (you cannot answer this one)" in out
    assert "`nf approve <approval_id>`" not in out
    assert "Waiting for the person at the desktop to answer in NevoFlux" in out


def test_task_wait_reads_desktop_only_from_each_result(home, capsys, monkeypatch):
    # Contract v1.2 R39: the person can flip "Let Muse approve sensitive actions" while the
    # approval waits; nothing about desktop_only is remembered from an earlier wait.
    desk = task_state("awaiting_approval", approvals=[
        approval_item(tool=None, choices=[], desktop_only=True)])
    fake = FakeBridge(monkeypatch, {"call": task_reply(desk)})
    assert main(["task", "wait", TASK_ID]) == 3
    assert "`nf approve <approval_id>`" not in capsys.readouterr().out
    fake.replies["call"] = task_reply(task_state("awaiting_approval", approvals=[
        approval_item("tool")]))
    assert main(["task", "wait", TASK_ID]) == 3
    out = capsys.readouterr().out
    assert "`nf approve <approval_id>`" in out
    assert "you cannot answer this one" not in out
    fake.replies["call"] = task_reply(desk)
    assert main(["task", "wait", TASK_ID]) == 3
    assert "you cannot answer this one" in capsys.readouterr().out


def test_task_wait_done_saves_the_screenshot(home, capsys, monkeypatch):
    done = task_state("done", reply="Here it is.")
    FakeBridge(monkeypatch, {"call": task_reply(done, image=JPEG_B64)})
    assert main(["task", "wait", TASK_ID, "--json"]) == 0
    printed = capsys.readouterr().out
    shot = home / "screenshots" / f"{TASK_ID}.jpg"
    assert json.loads(printed) == {**done, "screenshot": str(shot)}
    assert JPEG_B64 not in printed  # the image is a file, never base64 on stdout
    assert shot.read_bytes() == base64.b64decode(JPEG_B64)
    assert main(["task", "wait", TASK_ID]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"Task {TASK_ID}: done", "Here it is.", f"Screenshot: {shot}"]


def test_a_later_wait_names_the_saved_screenshot(home, capsys, monkeypatch):
    done = task_state("done", reply="Here it is.")
    fake = FakeBridge(monkeypatch, {"call": task_reply(done, image=JPEG_B64)})
    assert main(["task", "wait", TASK_ID, "--json"]) == 0
    capsys.readouterr()
    fake.replies["call"] = task_reply(done)  # R12: the image comes once per task and session
    assert main(["task", "wait", TASK_ID, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["screenshot"] == str(
        home / "screenshots" / f"{TASK_ID}.jpg")


def test_task_wait_done_without_a_screenshot(home, capsys, monkeypatch):
    done = task_state("done", reply="Done.", screenshot_error="tab_discarded")
    FakeBridge(monkeypatch, {"call": task_reply(done)})
    assert main(["task", "wait", TASK_ID, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == done
    assert not (home / "screenshots").exists()
    assert main(["task", "wait", TASK_ID]) == 0
    assert "No screenshot: tab_discarded" in capsys.readouterr().out


def test_task_wait_reports_an_unsaved_screenshot(home, capsys, monkeypatch):
    done = task_state("done", reply="Done.")
    FakeBridge(monkeypatch, {"call": task_reply(done, image="%%%")})
    assert main(["task", "wait", TASK_ID, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {**done, "screenshot_save_error": "bad_image"}


def test_a_hostile_task_id_never_becomes_a_path(home, capsys, monkeypatch):
    done = {**task_state("done", reply="Done."), "task_id": "../../outside"}
    FakeBridge(monkeypatch, {"call": task_reply(done, image=JPEG_B64)})
    assert main(["task", "wait", TASK_ID, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["screenshot_save_error"] == "bad_task_id"
    assert not (home / "screenshots").exists()
    assert not list(home.parent.glob("**/outside*"))


@pytest.mark.parametrize(("ended", "line"), [
    (task_state("failed", error={"code": "approval_timeout",
                                 "message": "nobody answered an approval within 30 minutes; "
                                            "it was denied"}),
     "approval_timeout: nobody answered an approval within 30 minutes; it was denied"),
    (task_state("cancelled"), f"Task {TASK_ID}: cancelled"),
])
def test_task_wait_failed_or_cancelled_exits_1(home, capsys, monkeypatch, ended, line):
    FakeBridge(monkeypatch, {"call": task_reply(ended)})
    assert main(["task", "wait", TASK_ID, "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == ended
    assert main(["task", "wait", TASK_ID]) == 1
    assert line in capsys.readouterr().out.splitlines()


def test_task_wait_for_an_unknown_task(home, capsys, monkeypatch):
    gone = tool_error("task_not_found", f"no task {TASK_ID} for this agent", task_id=TASK_ID)
    FakeBridge(monkeypatch, {"call": {"ok": True, "result": gone}})
    assert main(["task", "wait", TASK_ID, "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "task_not_found"


def test_task_wait_with_an_unreadable_result(home, capsys, monkeypatch):
    FakeBridge(monkeypatch, {"call": {"ok": True, "result": {"content": [], "isError": False}}})
    assert main(["task", "wait", TASK_ID, "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "bad_result"


def test_task_cancel(home, capsys, monkeypatch):
    fake = FakeBridge(monkeypatch, {"call": task_reply({"task_id": TASK_ID,
                                                        "state": "cancelled"})})
    assert main(["task", "cancel", TASK_ID, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"task_id": TASK_ID, "state": "cancelled"}
    assert fake.sent == [{"op": "call", "tool": "nevoflux_task_cancel",
                          "args": {"task_id": TASK_ID}, "timeout": 120.0}]
    assert main(["task", "cancel", TASK_ID]) == 0
    assert capsys.readouterr().out.strip() == f"Task {TASK_ID}: cancelled."


@pytest.mark.parametrize("session", ["", "   "])
def test_task_start_refuses_an_empty_session(home, capsys, monkeypatch, session):
    fake = FakeBridge(monkeypatch)
    assert main(["task", "start", "x", "--session", session]) == 2
    assert fake.sent == [] and "nf task start:" in capsys.readouterr().err


def test_task_ids_are_lowercased_before_sending(home, capsys, monkeypatch):
    fake = FakeBridge(monkeypatch, {"call": task_reply(task_state("running", steps=STEPS))})
    assert main(["task", "wait", TASK_ID.upper(), "--json"]) == 0
    assert main(["task", "cancel", TASK_ID.upper(), "--json"]) == 0
    assert [m["args"]["task_id"] for m in fake.sent] == [TASK_ID, TASK_ID]


@pytest.mark.parametrize("state_name", ["running", "awaiting_approval"])
def test_an_image_outside_a_done_result_is_not_saved(home, capsys, monkeypatch, state_name):
    FakeBridge(monkeypatch, {"call": task_reply(task_state(state_name), image=JPEG_B64)})
    assert main(["task", "wait", TASK_ID, "--json"]) == (3 if state_name != "running" else 0)
    out = json.loads(capsys.readouterr().out)
    assert "screenshot" not in out and "screenshot_save_error" not in out
    assert not (home / "screenshots").exists()


def test_a_task_command_when_the_bridge_fails(home, capsys, monkeypatch):
    FakeBridge(monkeypatch, {"call": OSError("bridge not running")})
    assert main(["task", "wait", TASK_ID, "--json"]) == 1
    assert "error" in json.loads(capsys.readouterr().out)


def test_a_task_reply_with_a_non_object_result(home, capsys, monkeypatch):
    FakeBridge(monkeypatch, {"call": {"ok": True, "result": "oops"}})
    assert main(["task", "cancel", TASK_ID, "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "bad_result"


def test_task_alone_is_a_usage_error(home, capsys):
    assert main(["task"]) == 2


@pytest.mark.parametrize("argv", [["task", "start", "x"], ["task", "wait", TASK_ID],
                                  ["task", "cancel", TASK_ID]])
def test_task_commands_on_windows(home, capsys, argv):
    assert main([*argv, "--json"]) == 1
    assert "unsupported on Windows" in json.loads(capsys.readouterr().out)["error"]


TASK_ANSWERED = {"ok": True, "state": "running"}


def test_approve_a_delegated_question(home, capsys, monkeypatch):
    fake = FakeBridge(monkeypatch, {"approve": task_reply(TASK_ANSWERED)})
    assert main(["approve", APPROVAL_ID, "--answer", "Blue", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == TASK_ANSWERED
    assert fake.sent == [{"op": "approve", "id": APPROVAL_ID, "timeout": 120.0,
                          "answer": "Blue"}]
    assert main(["approve", APPROVAL_ID]) == 0
    assert capsys.readouterr().out.strip() == (
        "Answered: the task is running. Follow it with `nf task wait`.")
    assert fake.sent[-1] == {"op": "approve", "id": APPROVAL_ID, "timeout": 120.0}


def test_answering_while_other_approvals_are_open(home, capsys, monkeypatch):
    FakeBridge(monkeypatch, {"approve": task_reply({"ok": True, "state": "awaiting_approval"})})
    assert main(["approve", APPROVAL_ID]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("Answered: the task is awaiting_approval.")
    assert "ther approvals may still be open" in out


def test_ids_are_lowercased_before_sending(home, capsys, monkeypatch):
    fake = FakeBridge(monkeypatch, {"deny": task_reply(TASK_ANSWERED)})
    assert main(["deny", APPROVAL_ID.upper(), "--task", TASK_ID.upper(), "--json"]) == 0
    assert fake.sent[0]["id"] == APPROVAL_ID and fake.sent[0]["task"] == TASK_ID


def test_deny_a_delegated_approval_with_its_task(home, capsys, monkeypatch):
    fake = FakeBridge(monkeypatch, {"deny": task_reply(TASK_ANSWERED)})
    assert main(["deny", APPROVAL_ID, "--task", TASK_ID, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == TASK_ANSWERED
    assert fake.sent == [{"op": "deny", "id": APPROVAL_ID, "timeout": 120.0, "task": TASK_ID}]


@pytest.mark.parametrize("flag", ["--always", "--allow-always", "--choice=allow_always"])
def test_allow_always_is_never_offered(home, capsys, monkeypatch, flag):
    fake = FakeBridge(monkeypatch, {"approve": task_reply(TASK_ANSWERED)})
    with pytest.raises(SystemExit) as e:
        main(["approve", APPROVAL_ID, flag])
    assert e.value.code == 2 and fake.sent == []


def test_answer_only_goes_with_approve(home, capsys, monkeypatch):
    fake = FakeBridge(monkeypatch)
    with pytest.raises(SystemExit) as e:
        main(["deny", APPROVAL_ID, "--answer", "x"])
    assert e.value.code == 2 and fake.sent == []


def test_direct_approvals_refuse_task_flags(home, capsys, monkeypatch):
    fake = FakeBridge(monkeypatch)
    assert main(["approve", "3f9a1c2e", "--answer", "x"]) == 2
    assert main(["deny", "3f9a1c2e", "--task", TASK_ID]) == 2
    assert fake.sent == []
    assert "delegated task" in capsys.readouterr().err


def test_answer_length(home, capsys, monkeypatch):
    fake = FakeBridge(monkeypatch, {"approve": task_reply(TASK_ANSWERED)})
    assert main(["approve", APPROVAL_ID, "--answer", "a" * 4001]) == 2
    assert fake.sent == []
    assert main(["approve", APPROVAL_ID, "--answer", "a" * 4000, "--json"]) == 0
    assert len(fake.sent[0]["answer"]) == 4000


@pytest.mark.parametrize(("code", "extra"), [("approval_not_found", {"approval_id": APPROVAL_ID}),
                                             ("approval_desktop_only", {"approval_id": APPROVAL_ID}),
                                             ("task_not_found", {"task_id": TASK_ID})])
def test_delegated_answer_errors_print_their_code(home, capsys, monkeypatch, code, extra):
    FakeBridge(monkeypatch, {"approve": {"ok": True,
                                         "result": tool_error(code, "gone", **extra)}})
    assert main(["approve", APPROVAL_ID, "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": code, "message": "gone", **extra}


def test_task_required_is_printed_code_first(home, capsys, monkeypatch):
    FakeBridge(monkeypatch, {"approve": {"ok": False, "error": {
        "code": "task_required", "message": "pass the task id with --task"}}})
    assert main(["approve", APPROVAL_ID, "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "error": "task_required", "message": "pass the task id with --task"}
    assert main(["approve", APPROVAL_ID]) == 1
    assert "nf: task_required: pass the task id with --task" in capsys.readouterr().err
