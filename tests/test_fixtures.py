import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures" / "muse"
sys.path.insert(0, str(ROOT / "scripts"))

import check_fixtures  # noqa: E402

NAMES = ["PROTOCOL_VERSION", "kdf.json", "pairing_code.json", "seal.json",
         "envelope.json", "mcp_messages.json"]


def test_every_fixture_is_present():
    assert sorted(p.name for p in FIX.iterdir()) == sorted(NAMES)


def test_fixture_files_have_no_carriage_returns():
    for name in NAMES:
        assert b"\r" not in (FIX / name).read_bytes(), f"{name} was rewritten with CRLF"


def test_protocol_version_matches_the_client():
    from nevoflux_muse import PROTOCOL_MAX, PROTOCOL_MIN
    version = int((FIX / "PROTOCOL_VERSION").read_text().strip())
    assert PROTOCOL_MIN <= version <= PROTOCOL_MAX


def test_pairing_vectors_are_the_agreed_ten():
    data = json.loads((FIX / "pairing_code.json").read_text(encoding="utf-8"))
    assert len(data["valid"]) + len(data["invalid"]) == 10


def test_source_pins_a_commit_not_a_branch():
    src = json.loads((ROOT / "fixtures" / "SOURCE.json").read_text())
    assert len(src["commit"]) == 40 and all(c in "0123456789abcdef" for c in src["commit"])


def test_identical_copies_compare_clean():
    files = {n: (FIX / n).read_bytes() for n in NAMES}
    assert check_fixtures.compare(files, dict(files)) == []


def test_protocol_bump_is_reported_first():
    local = {n: (FIX / n).read_bytes() for n in NAMES}
    upstream = dict(local)
    upstream["PROTOCOL_VERSION"] = b"2\n"
    upstream["envelope.json"] = b"{}"
    problems = check_fixtures.compare(local, upstream)
    assert len(problems) == 1
    assert "protocol" in problems[0].lower() and "sync" in problems[0].lower()


def test_a_byte_difference_names_the_file():
    local = {n: (FIX / n).read_bytes() for n in NAMES}
    upstream = dict(local)
    upstream["seal.json"] = local["seal.json"] + b" "
    assert check_fixtures.compare(local, upstream) == ["seal.json differs from upstream"]


def test_missing_remote_commit_is_explained(monkeypatch):
    def not_found(url):
        raise check_fixtures.NotFound(url)
    monkeypatch.setattr(check_fixtures, "_get", not_found)
    with pytest.raises(SystemExit) as exc:
        check_fixtures.fetch_remote("nevoflux-browser/nevoflux-agent", "0" * 40,
                                    "crates/daemon/tests/fixtures/muse", NAMES)
    assert "pushed" in str(exc.value)
