import json
from pathlib import Path

import pytest

from nevoflux_muse import PROTOCOL_MAX, PROTOCOL_MIN, __version__
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
    assert main(["reset", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "removed": [ACCOUNT_FILE, PAIRING_FILE], "left": [], "state_dir": str(home)}
    assert not home.exists()


def test_reset_with_nothing_there(home, capsys):
    assert main(["reset", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "removed": [], "left": [], "state_dir": str(home)}


def test_reset_leaves_files_it_does_not_know(home, capsys):
    _populate(home)
    (home / "notes.txt").write_text("mine")
    assert main(["reset", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["removed"] == [ACCOUNT_FILE, PAIRING_FILE]
    assert data["left"] == ["notes.txt"]
    assert (home / "notes.txt").read_text() == "mine"


def test_reset_failure_is_reported(home, capsys, monkeypatch):
    _populate(home)

    def refuse(self, missing_ok=False):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "unlink", refuse)
    assert main(["reset", "--json"]) == 1
    assert "denied" in json.loads(capsys.readouterr().out)["error"]
