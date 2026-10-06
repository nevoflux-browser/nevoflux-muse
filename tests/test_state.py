import json
import sys
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


def test_write_and_read_secret(tmp_path):
    d = tmp_path / "nf"
    state.write_secret(d, state.PAIRING_FILE, {"relay": "wss://r", "channel": "c"})
    assert state.read_state(d, state.PAIRING_FILE,
                            {"relay": str, "channel": str}) == {
        "relay": "wss://r", "channel": "c", "v": 1}
    assert [p.name for p in d.iterdir()] == [state.PAIRING_FILE]  # no temp file left


def test_read_state_absent(tmp_path):
    assert state.read_state(tmp_path, state.ACCOUNT_FILE) is None


def test_write_secret_replaces(tmp_path):
    state.write_secret(tmp_path, state.ACCOUNT_FILE, {"a": 1})
    state.write_secret(tmp_path, state.ACCOUNT_FILE, {"a": 2})
    assert state.read_state(tmp_path, state.ACCOUNT_FILE)["a"] == 2


@pytest.mark.parametrize("content", ["", "{", "[1]", '{"v": 2}', '{"a": 1}'])
def test_read_state_rejects_garbage(tmp_path, content):
    (tmp_path / state.ACCOUNT_FILE).write_text(content)
    with pytest.raises(state.UnsupportedState) as e:
        state.read_state(tmp_path, state.ACCOUNT_FILE)
    assert state.ACCOUNT_FILE in str(e.value)
    assert "nf reset --local-only" in str(e.value)


def test_read_state_requires_fields(tmp_path):
    state.write_secret(tmp_path, state.ACCOUNT_FILE, {"base_url": "x"})
    with pytest.raises(state.UnsupportedState):
        state.read_state(tmp_path, state.ACCOUNT_FILE, {"base_url": str, "access_token": str})


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_secret_permissions(tmp_path):
    d = tmp_path / "nf"
    state.write_secret(d, state.ACCOUNT_FILE, {"a": 1})
    assert d.stat().st_mode & 0o777 == 0o700
    assert (d / state.ACCOUNT_FILE).stat().st_mode & 0o777 == 0o600


def test_reset_can_keep_a_file(tmp_path):
    for name in state.KNOWN_FILES:
        state.write_secret(tmp_path, name, {})
    removed, left = state.reset(tmp_path, keep=(state.ACCOUNT_FILE,))
    assert removed == [state.AUTH_PENDING_FILE, state.PAIRING_FILE]
    assert left == [state.ACCOUNT_FILE]
    assert tmp_path.exists()


def test_written_json_is_a_plain_object(tmp_path):
    state.write_secret(tmp_path, state.PAIRING_FILE, {"k": "v"})
    assert json.loads((tmp_path / state.PAIRING_FILE).read_text()) == {"k": "v", "v": 1}


def test_read_state_invalid_utf8(tmp_path):
    (tmp_path / state.ACCOUNT_FILE).write_bytes(b"\xff\xfe")
    with pytest.raises(state.UnsupportedState):
        state.read_state(tmp_path, state.ACCOUNT_FILE)


@pytest.mark.parametrize("value", [5, None, True, ["x"]])
def test_read_state_checks_field_types(tmp_path, value):
    state.write_secret(tmp_path, state.ACCOUNT_FILE, {"base_url": value})
    with pytest.raises(state.UnsupportedState):
        state.read_state(tmp_path, state.ACCOUNT_FILE, {"base_url": str})


@pytest.mark.parametrize("value", [3, 2.5])
def test_read_state_numbers_are_int_or_float(tmp_path, value):
    state.write_secret(tmp_path, state.ACCOUNT_FILE, {"interval": value})
    got = state.read_state(tmp_path, state.ACCOUNT_FILE, {"interval": state.NUMBER})
    assert got["interval"] == value


@pytest.mark.parametrize("value", ["soon", True, None])
def test_read_state_numbers_reject_the_rest(tmp_path, value):
    state.write_secret(tmp_path, state.ACCOUNT_FILE, {"interval": value})
    with pytest.raises(state.UnsupportedState):
        state.read_state(tmp_path, state.ACCOUNT_FILE, {"interval": state.NUMBER})


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_read_state_rejects_non_finite_numbers(tmp_path, value):
    (tmp_path / state.AUTH_PENDING_FILE).write_text(f'{{"v": 1, "interval": {value}}}')
    with pytest.raises(state.UnsupportedState):
        state.read_state(tmp_path, state.AUTH_PENDING_FILE, {"interval": state.NUMBER})
