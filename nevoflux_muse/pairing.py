"""The pairing block, and the pairing it leaves behind.

The person runs /pair-agent in the NevoFlux sidebar and pastes the block into
the agent, which pipes it to `nf pair --from-stdin` (protocol §1):

    NEVOFLUX_AGENT_PAIRING
    relay: wss://relay.nevoflux.app
    channel: <uuid>
    code: A-BCDE-FGHJ-KMNP

Values are read by prefix, so the chat noise around a pasted block (a sentence
before it, code fences, CRLF) does not matter. Only the derived key is stored,
never the code.
"""

from __future__ import annotations

import base64
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import crypto, state

PAIRING_FIELDS = {"relay": str, "channel": str, "key": str}
_LOCAL_HOSTS = ("127.0.0.1", "localhost")


class InvalidPairingBlock(ValueError):
    pass


@dataclass(frozen=True)
class PairingBlock:
    relay: str
    channel: str
    code: str


@dataclass(frozen=True)
class Pairing:
    relay: str
    channel: str
    key: bytes


def _check_relay(relay: str) -> str:
    url = urllib.parse.urlparse(relay)
    if url.scheme == "wss" and url.hostname:
        return relay
    if url.scheme == "ws" and url.hostname in _LOCAL_HOSTS:
        return relay  # a local test relay
    raise InvalidPairingBlock("relay: must be a wss:// URL")


def parse_block(text: str) -> PairingBlock:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition(":")
        key = key.strip().lower()
        if sep and key in ("relay", "channel", "code"):
            fields.setdefault(key, value.strip())
    for name in ("relay", "channel", "code"):
        if not fields.get(name):
            raise InvalidPairingBlock(f"the pairing block has no {name}: "
                                      "line")
    return PairingBlock(
        relay=_check_relay(fields["relay"].rstrip("/")),
        channel=fields["channel"],
        code=crypto.normalize_code(fields["code"]),
    )


def pair(text: str, d: Path) -> PairingBlock:
    """Replace the pairing. The account token is kept: it is not tied to a
    pairing."""
    block = parse_block(text)
    key = crypto.derive_channel_key(block.code, block.channel)
    state.write_secret(d, state.PAIRING_FILE, {
        "relay": block.relay,
        "channel": block.channel,
        "key": base64.b64encode(key).decode(),
    })
    return block


def load_pairing(d: Path) -> Pairing | None:
    obj = state.read_state(d, state.PAIRING_FILE, PAIRING_FIELDS)
    if obj is None:
        return None
    try:
        key = base64.b64decode(obj["key"], validate=True)
    except ValueError:  # binascii.Error
        raise state.UnsupportedState(state.PAIRING_FILE) from None
    if len(key) != 32:
        raise state.UnsupportedState(state.PAIRING_FILE)
    return Pairing(obj["relay"], obj["channel"], key)
