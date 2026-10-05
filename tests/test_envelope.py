import json
from pathlib import Path

import pytest

from nevoflux_muse.envelope import Challenge, Envelope, Message, NoChallenge, Rejected

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "muse"
ENV = json.loads((FIX / "envelope.json").read_text(encoding="utf-8"))
MCP = json.loads((FIX / "mcp_messages.json").read_text(encoding="utf-8"))
CH = ENV["challenge"]


def head_check(frame, challenge, last):
    """The head's inbound rules (protocol §4), as a reference for what we send.

    Returns (verdict, new_last). Order: shape, direction, challenge, counter, m.
    """
    if not (isinstance(frame, dict) and {"d", "n", "ch", "m"} <= frame.keys()):
        return "reject:malformed", last
    n = frame["n"]
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        return "reject:malformed", last
    if frame["d"] != "c2h":
        return "reject:wrong_direction", last
    if frame["ch"] != challenge:
        return "reject:challenge_mismatch", last
    if n <= last:
        return "reject:counter_regression", last
    if not isinstance(frame["m"], dict):
        return "reject:malformed", last
    return "accept", n


@pytest.mark.parametrize("case", ENV["cases"], ids=lambda c: c["name"])
def test_reference_checker_agrees_with_the_fixture(case):
    last = -1
    for prior in case["prior"]:
        verdict, last = head_check(prior, CH, last)
        assert verdict == "accept"
    assert head_check(case["frame"], CH, last)[0] == case["expect"]


def challenge(ch=CH):
    return {"d": "h2c", "n": 0, "ch": ch, "m": None}


def h2c(n, m=None, ch=CH):
    return {
        "d": "h2c",
        "n": n,
        "ch": ch,
        "m": {"jsonrpc": "2.0", "id": n} if m is None else m
    }


def test_outbound_frames_pass_the_head():
    env = Envelope()
    assert env.inbound(challenge()) == Challenge(CH)
    last = -1
    for m in MCP["requests"]:
        frame = env.outbound(m)
        assert frame["d"] == "c2h" and frame["m"] == m
        verdict, last = head_check(frame, CH, last)
        assert verdict == "accept"
    assert last == len(MCP["requests"]) - 1


def test_outbound_needs_a_challenge():
    with pytest.raises(NoChallenge):
        Envelope().outbound(
            {"jsonrpc": "2.0", "method": "ping", "id": 1}
        )


def test_no_challenge_yet():
    assert Envelope().inbound(h2c(1)) == Rejected("no_challenge")


def test_counter_rules():
    env = Envelope()
    env.inbound(challenge())
    assert env.inbound(h2c(1)) == Message({"jsonrpc": "2.0", "id": 1})
    assert env.inbound(h2c(1)) == Rejected("counter_regression")
    assert isinstance(env.inbound(h2c(5)), Message)  # gaps are fine
    assert env.inbound(h2c(3)) == Rejected("counter_regression")


def test_direction_and_challenge():
    env = Envelope()
    env.inbound(challenge())
    assert (
        env.inbound({**h2c(1), "d": "c2h"})
        == Rejected("wrong_direction")
    )
    assert (
        env.inbound(h2c(1, ch="ZZZZZZZZZZZZZZZZZZZZZZ"))
        == Rejected("challenge_mismatch")
    )


@pytest.mark.parametrize("frame", [
    "hello",
    {"d": "h2c", "n": 1, "ch": CH},
    {"d": "h2c", "n": -1, "ch": CH, "m": {}},
    {"d": "h2c", "n": True, "ch": CH, "m": {}},
    {"d": "h2c", "n": 1, "ch": CH, "m": None},
    {"d": "h2c", "n": 1, "ch": CH, "m": [1]},
    {"d": "h2c", "n": 0, "ch": 7, "m": None},
])
def test_malformed(frame):
    env = Envelope()
    env.inbound(challenge())
    assert env.inbound(frame) == Rejected("malformed")


def test_new_challenge_mid_session_resets_counters():
    env = Envelope()
    env.inbound(challenge())
    env.outbound({"a": 1})
    env.inbound(h2c(4))
    assert env.inbound(challenge("NEW")) == Challenge("NEW")
    assert env.outbound({"a": 2})["n"] == 0
    assert isinstance(env.inbound(h2c(1, ch="NEW")), Message)
    assert env.inbound(h2c(2)) == Rejected("challenge_mismatch")


def test_replayed_challenge_is_stale_even_on_a_new_connection():
    seen: set[str] = set()
    first = Envelope(seen)
    first.inbound(challenge())
    first.inbound(challenge("NEW"))
    assert first.inbound(challenge()) == Rejected("stale_challenge")
    assert first.challenge == "NEW"
    second = Envelope(seen)  # the next connection shares the record
    assert second.inbound(challenge("NEW")) == Rejected(
        "stale_challenge"
    )
    assert second.challenge is None
