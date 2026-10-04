from pathlib import Path

import pytest

from nevoflux_muse import state


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for var in ("NF_HOME", "LOCALAPPDATA", "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    return tmp_path


def test_nf_home_wins_everywhere(monkeypatch, clean_env):
    monkeypatch.setenv("NF_HOME", str(clean_env / "custom"))
    for platform in ("win32", "darwin", "linux"):
        monkeypatch.setattr(state.sys, "platform", platform)
        assert state.state_dir() == clean_env / "custom"


def test_empty_nf_home_is_ignored(monkeypatch, clean_env):
    monkeypatch.setenv("NF_HOME", "")
    monkeypatch.setattr(state.sys, "platform", "darwin")
    assert state.state_dir() == clean_env / "home" / "Library" / "Application Support" / "nevoflux-muse"


def test_windows_uses_local_app_data(monkeypatch, clean_env):
    monkeypatch.setattr(state.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(clean_env / "lad"))
    assert state.state_dir() == clean_env / "lad" / "nevoflux-muse"


def test_windows_without_local_app_data(monkeypatch, clean_env):
    monkeypatch.setattr(state.sys, "platform", "win32")
    assert state.state_dir() == clean_env / "home" / "AppData" / "Local" / "nevoflux-muse"


def test_linux_uses_xdg_state_home(monkeypatch, clean_env):
    monkeypatch.setattr(state.sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", str(clean_env / "xdg"))
    assert state.state_dir() == clean_env / "xdg" / "nevoflux-muse"


def test_linux_ignores_a_relative_xdg_state_home(monkeypatch, clean_env):
    # The XDG spec says relative paths are invalid and must be ignored.
    monkeypatch.setattr(state.sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
    assert state.state_dir() == clean_env / "home" / ".local" / "state" / "nevoflux-muse"
