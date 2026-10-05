"""Pairing-code normalization, the channel key, and sealing.

All three are pinned byte for byte by fixtures/muse/ (pairing_code.json,
kdf.json, seal.json); when this module and the protocol document disagree, the
fixtures win.
"""

from __future__ import annotations

import json
import os
import re

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
NONCE_LEN = 12
TAG_LEN = 16

_NOT_ASCII_ALNUM = re.compile(r"[^0-9A-Za-z]")
_LOOKALIKES = str.maketrans("ILO", "110")


class InvalidPairingCode(ValueError):
    pass


def normalize_code(raw: str) -> str:
    """The canonical X-XXXX-XXXX-XXXX form, or InvalidPairingCode. Never guesses."""
    s = _NOT_ASCII_ALNUM.sub("", raw).upper().translate(_LOOKALIKES)
    if len(s) != 13 or any(c not in ALPHABET for c in s):
        # The message never echoes the input: it is a secret.
        raise InvalidPairingCode(f"a pairing code is 13 characters from {ALPHABET}")
    return f"{s[0]}-{s[1:5]}-{s[5:9]}-{s[9:]}"


def derive_channel_key(code: str, channel: str) -> bytes:
    return hash_secret_raw(
        secret=code.encode(),
        salt=f"{code}|{channel}".encode(),
        time_cost=3,
        memory_cost=65536,
        parallelism=4,
        hash_len=32,
        type=Type.ID,
        version=19,
    )


def seal(key: bytes, msg: dict, nonce: bytes | None = None) -> bytes:
    nonce = os.urandom(NONCE_LEN) if nonce is None else nonce
    # Compact and in insertion order, like the head's serde_json.
    plaintext = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode()
    return nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def open_sealed(key: bytes, data: bytes) -> dict | None:
    """The message, or None for anything that does not open: such messages are dropped."""
    if len(data) < NONCE_LEN + TAG_LEN:
        return None
    try:
        plaintext = AESGCM(key).decrypt(data[:NONCE_LEN], data[NONCE_LEN:], None)
        msg = json.loads(plaintext)
    except (InvalidTag, ValueError):
        return None
    return msg if isinstance(msg, dict) else None
