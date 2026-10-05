import json
from pathlib import Path

import pytest

from nevoflux_muse import crypto

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "muse"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", load("pairing_code.json")["valid"], ids=lambda c: c["input"])
def test_normalize_valid(case):
    assert crypto.normalize_code(case["input"]) == case["canonical"]


@pytest.mark.parametrize("raw", load("pairing_code.json")["invalid"])
def test_normalize_invalid(raw):
    with pytest.raises(crypto.InvalidPairingCode) as e:
        crypto.normalize_code(raw)
    assert raw not in str(e.value)


def test_alphabet_matches_fixture():
    assert crypto.ALPHABET == load("pairing_code.json")["alphabet"]


def test_unicode_digits_are_not_alphanumerics():
    # str.isalnum() would keep these; the rule is ASCII [0-9A-Za-z] only.
    with pytest.raises(crypto.InvalidPairingCode):
        crypto.normalize_code("X-ABCD-EFGH-JKM١")


@pytest.mark.parametrize("v", load("kdf.json")["vectors"], ids=lambda v: v["channel_id"])
def test_kdf_vectors(v):
    assert crypto.derive_channel_key(v["code"], v["channel_id"]).hex() == v["key_hex"]


def test_seal_vector_opens():
    f = load("seal.json")
    assert crypto.open_sealed(bytes.fromhex(f["key_hex"]), bytes.fromhex(f["sealed_hex"])) == f["plaintext"]


def test_seal_vector_reproduces():
    f = load("seal.json")
    sealed = crypto.seal(bytes.fromhex(f["key_hex"]), f["plaintext"], bytes.fromhex(f["nonce_hex"]))
    assert sealed.hex() == f["sealed_hex"]


def test_seal_uses_fresh_nonces():
    key = bytes(32)
    a, b = crypto.seal(key, {"k": 1}), crypto.seal(key, {"k": 1})
    assert a[:12] != b[:12]
    assert crypto.open_sealed(key, a) == {"k": 1}


@pytest.mark.parametrize("data", [b"", b"short", bytes(40)])
def test_open_rejects_junk(data):
    assert crypto.open_sealed(bytes(32), data) is None


def test_open_rejects_wrong_key_and_tampering():
    sealed = bytearray(crypto.seal(bytes(32), {"k": 1}))
    assert crypto.open_sealed(bytes([1] * 32), bytes(sealed)) is None
    sealed[-1] ^= 1
    assert crypto.open_sealed(bytes(32), bytes(sealed)) is None


def test_open_rejects_non_object_plaintext():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = bytes(12)
    data = nonce + AESGCM(bytes(32)).encrypt(nonce, b"[1,2]", None)
    assert crypto.open_sealed(bytes(32), data) is None
