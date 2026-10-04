import json

from nevoflux_muse import PROTOCOL_MAX, PROTOCOL_MIN, __version__
from nevoflux_muse.cli import main


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
