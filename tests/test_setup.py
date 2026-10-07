import json
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
    text = daemon.unit_text(tmp_path)
    assert "ExecStart=" in text and text.splitlines()[text.splitlines().index("[Service]") + 1] \
        .endswith(" daemon")
    for line in ("Restart=on-failure", "RestartSec=5", "RestartPreventExitStatus=3",
                 "WantedBy=default.target"):
        assert line in text.splitlines()
    assert "Environment=NF_HOME" not in text


def test_unit_text_carries_nf_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NF_HOME", str(tmp_path / "s"))
    assert f"Environment=NF_HOME={tmp_path / 's'}" in daemon.unit_text(tmp_path / "s")


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
