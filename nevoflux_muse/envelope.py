"""The agent's side of the envelope (protocol §4).

The head speaks first on every connection with a challenge frame
(d=h2c, n=0, m=null). Every frame after that carries the challenge, and each
direction counts up from 0 on its own. A new challenge can arrive at any time
(the head restarted): it starts a new session and both counters restart.

A challenge value is accepted once. The relay sees every sealed frame and could
replay an old challenge, then the frames that followed it, to walk the client
back into a stale session; `seen` remembers every challenge across connections
so that a replay is refused.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Challenge:
    ch: str


@dataclass(frozen=True)
class Message:
    m: dict


@dataclass(frozen=True)
class Rejected:
    reason: str


class NoChallenge(RuntimeError):
    pass


def _shaped(frame: object) -> bool:
    if not (isinstance(frame, dict) and {"d", "n", "ch", "m"} <= frame.keys()):
        return False
    n = frame["n"]
    return (
        isinstance(n, int)
        and not isinstance(n, bool)
        and n >= 0
        and isinstance(frame["ch"], str)
    )


class Envelope:
    def __init__(self, seen: set[str] | None = None):
        self.challenge: str | None = None
        self._seen = set() if seen is None else seen
        self._next_out = 0
        self._last_in = 0

    def outbound(self, m: dict) -> dict:
        if self.challenge is None:
            raise NoChallenge("the head has not sent a challenge yet")
        frame = {
            "d": "c2h",
            "n": self._next_out,
            "ch": self.challenge,
            "m": m
        }
        self._next_out += 1
        return frame

    def inbound(self, frame: object) -> Challenge | Message | Rejected:
        if not _shaped(frame):
            return Rejected("malformed")
        if frame["d"] != "h2c":
            return Rejected("wrong_direction")
        if frame["n"] == 0 and frame["m"] is None:
            if frame["ch"] in self._seen:
                return Rejected("stale_challenge")
            self._seen.add(frame["ch"])
            self.challenge = frame["ch"]
            self._next_out = 0
            self._last_in = 0
            return Challenge(frame["ch"])
        if self.challenge is None:
            return Rejected("no_challenge")
        if frame["ch"] != self.challenge:
            return Rejected("challenge_mismatch")
        if frame["n"] <= self._last_in:
            return Rejected("counter_regression")
        if not isinstance(frame["m"], dict):
            return Rejected("malformed")
        self._last_in = frame["n"]
        return Message(frame["m"])
