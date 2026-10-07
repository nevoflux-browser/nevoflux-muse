import json
import os
import sys
from pathlib import Path

import pytest
from conftest import DEAD_URL

from nevoflux_muse import daemon, state
from nevoflux_muse.cli import main

can_symlink = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges")


@pytest.fixture
def box(tmp_path, monkeypatch, account):
    d, root, home = tmp_path / "state", tmp_path / "root", tmp_path / "home"
    monkeypatch.setenv("NF_HOME", str(d))
    monkeypatch.setenv("NF_INSTALL_ROOT", str(root))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(daemon, "SUPPORTED", False)  # held() is a no-op off POSIX
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "bin" / "nf").write_text("")
    (root / "installed_tag").write_text("v0.2.0\n")
    account.valid.add("tok-1")
    state.write_secret(d, state.ACCOUNT_FILE, {"base_url": account.url, "access_token": "tok-1"})
    state.write_secret(d, state.PAIRING_FILE, {"relay": "wss://r", "channel": "c", "key": "AA=="})
    return type("Box", (), {"d": d, "root": root, "home": home, "account": account})


def run(capsys):
    code = main(["uninstall", "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_uninstall_removes_everything(box, capsys):
    code, out = run(capsys)
    assert code == 0 and out["ok"] is True
    assert [s["step"] for s in out["steps"]] == ["revoke", "state", "install_root", "link"]
    assert box.account.valid == set()
    assert not box.root.exists() and not box.d.exists()
    assert any("/unpair" in s for s in out["next_steps"])
    assert any("ensure.sh" in s for s in out["next_steps"])


def test_uninstall_stops_when_revoke_fails(box, capsys):
    state.write_secret(box.d, state.ACCOUNT_FILE, {"base_url": DEAD_URL, "access_token": "tok-1"})
    code, out = run(capsys)
    assert code == 1 and out["ok"] is False
    assert out["steps"][-1]["step"] == "revoke" and out["steps"][-1]["ok"] is False
    assert box.root.exists()  # the install stays so the token can be revoked later
    assert (box.d / state.ACCOUNT_FILE).exists()


def test_uninstall_without_setup_sh(box, capsys, monkeypatch):
    monkeypatch.delenv("NF_INSTALL_ROOT")
    code, out = run(capsys)
    assert code == 0
    step = next(s for s in out["steps"] if s["step"] == "install_root")
    assert step["ok"] and "setup.sh" in step["skipped"]
    assert box.root.exists()


def test_uninstall_removes_the_systemd_unit(box, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(daemon, "remove_unit", lambda: calls.append("remove"))
    monkeypatch.setattr(daemon, "SUPPORTED", True)
    monkeypatch.setattr(daemon, "stop", lambda d, timeout=5.0: False)
    state.write_supervision(box.d, "systemd-user")
    if sys.platform == "win32":
        pytest.skip("held() needs fcntl")
    code, out = run(capsys)
    assert code == 0 and calls == ["remove"]
    assert out["steps"][0]["step"] == "service"


@can_symlink
def test_uninstall_removes_our_link(box, capsys):
    (box.home / ".local" / "bin").mkdir(parents=True)
    link = box.home / ".local" / "bin" / "nf"
    os.symlink(box.root / "venv" / "bin" / "nf", link)
    code, _out = run(capsys)
    assert code == 0 and not os.path.lexists(link)


@can_symlink
def test_uninstall_leaves_a_foreign_link(box, capsys, tmp_path):
    (box.home / ".local" / "bin").mkdir(parents=True)
    link = box.home / ".local" / "bin" / "nf"
    other = tmp_path / "other-nf"
    other.write_text("")
    os.symlink(other, link)
    code, out = run(capsys)
    assert code == 0 and os.path.lexists(link)
    assert next(s for s in out["steps"] if s["step"] == "link").get("skipped")
