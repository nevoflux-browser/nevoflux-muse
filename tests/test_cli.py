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
    assert run_json(capsys, "status")[1] == {"state": "ready", "paired": True, "auth": "ok"}
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
