import json
import sys
from pathlib import Path

import pytest

from nevoflux_muse import daemon, state
from nevoflux_muse.cli import main


@pytest.fixture
def env(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setenv("NF_HOME", str(d))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(daemon, "SUPPORTED", True)
    calls = []
    codes = {}
    monkeypatch.setattr(daemon, "run_systemctl", lambda *a: calls.append(a) or codes.get(a, 0))
    bridge = {"alive": None, "ready": False, "ensured": 0, "stopped": 0}
    monkeypatch.setattr(daemon, "alive", lambda d: bridge["alive"])
    monkeypatch.setattr(daemon, "ready", lambda d: bridge["ready"])

    def ensure(d):
        bridge["ensured"] += 1
        return True

    def stop(d, timeout=5.0):
        bridge["stopped"] += 1
        return True

    monkeypatch.setattr(daemon, "ensure", ensure)
    monkeypatch.setattr(daemon, "stop", stop)
    return type("Env", (), {"d": d, "calls": calls, "codes": codes, "bridge": bridge,
                            "home": tmp_path / "home"})


def test_unit_text(tmp_path, monkeypatch):
    monkeypatch.delenv("NF_HOME", raising=False)
    lines = daemon.unit_text(tmp_path).splitlines()
    assert f'ExecStart="{sys.executable}" -m nevoflux_muse.cli daemon' in [
        line.replace("\\\\", "\\") for line in lines]
    for line in ("Restart=on-failure", "RestartSec=5", "RestartPreventExitStatus=3",
                 "WantedBy=default.target"):
        assert line in lines
    assert not any(line.startswith("Environment=") for line in lines)


def test_unit_text_carries_nf_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NF_HOME", str(tmp_path / "s"))
    path = str(tmp_path / "s").replace("\\", "\\\\")
    assert f'Environment="NF_HOME={path}"' in daemon.unit_text(tmp_path / "s").splitlines()


def test_unit_text_quotes_spaces_and_percent(monkeypatch):
    monkeypatch.setenv("NF_HOME", "/a b/50%/q\"x")
    monkeypatch.setattr(sys, "executable", "/opt/my py/bin/python")
    lines = daemon.unit_text(Path("/a b/50%/q\"x")).splitlines()
    assert 'ExecStart="/opt/my py/bin/python" -m nevoflux_muse.cli daemon' in lines
    home = str(Path("/a b/50%/q\"x")).replace("\\", "\\\\")  # backslashes (Windows) doubled
    assert 'Environment="NF_HOME=' + home.replace('"', '\\"').replace("%", "%%") + '"' in lines


def test_setup_with_systemd(env):
    result = daemon.setup(env.d)
    assert result == {"supervision": "systemd-user", "started": False,
                      "unit": str(env.home / ".config/systemd/user/nevoflux-muse.service")}
    assert env.calls == [("show-environment",), ("daemon-reload",),
                         ("enable", "nevoflux-muse.service")]
    assert Path(result["unit"]).read_text() == daemon.unit_text(env.d)
    assert state.supervision(env.d) == "systemd-user"
    assert env.bridge["ensured"] == 0  # not ready: nothing to start


def test_setup_without_systemd(env):
    env.codes[("show-environment",)] = 1
    env.bridge["ready"] = True
    result = daemon.setup(env.d)
    assert result == {"supervision": "detached+watchdog", "started": True, "unit": None}
    assert env.calls == [("show-environment",)]
    assert state.supervision(env.d) == "detached+watchdog"


def test_setup_no_service_skips_detection(env):
    assert daemon.setup(env.d, service=False)["supervision"] == "detached+watchdog"
    assert env.calls == []


def test_rerun_restarts_a_running_bridge(env):
    env.bridge.update(alive={"ok": True, "pid": 1}, ready=True)
    daemon.setup(env.d, service=False)
    assert env.bridge["stopped"] == 1 and env.bridge["ensured"] == 1


def test_enable_failure_is_reported(env):
    env.codes[("enable", "nevoflux-muse.service")] = 1
    with pytest.raises(Exception, match="enable"):
        daemon.setup(env.d)


def test_remove_unit(env):
    daemon.setup(env.d)
    env.calls.clear()
    daemon.remove_unit()
    assert env.calls == [("disable", "--now", "nevoflux-muse.service"), ("daemon-reload",)]
    assert not daemon.unit_path().exists()


def test_cli_setup(env, capsys):
    env.codes[("show-environment",)] = 1
    assert main(["setup", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["supervision"] == "detached+watchdog"
    assert main(["setup", "--no-service"]) == 0
    assert "detached+watchdog" in capsys.readouterr().out


def test_cli_setup_on_windows(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NF_HOME", str(tmp_path))
    monkeypatch.setattr(daemon, "SUPPORTED", False)
    assert main(["setup", "--json"]) == 1
    assert "unsupported on Windows" in json.loads(capsys.readouterr().out)["error"]


def test_switching_to_no_service_removes_the_old_unit(env):
    daemon.setup(env.d)
    env.calls.clear()
    daemon.setup(env.d, service=False)
    assert not daemon.unit_path().exists()
    assert ("disable", "--now", "nevoflux-muse.service") in env.calls


def test_unwritable_unit_is_an_error_not_a_traceback(env, monkeypatch, capsys):
    def deny(self, *a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "write_text", deny)
    assert main(["setup", "--json"]) == 1
    assert "cannot write" in json.loads(capsys.readouterr().out)["error"]
