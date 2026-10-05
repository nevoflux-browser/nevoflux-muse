import base64

import pytest

from nevoflux_muse import crypto, pairing, state

CHANNEL = "2f1c4a90-7b3e-4d1a-9c58-0e6a2b7d4f31"
BLOCK = (
    "NEVOFLUX_AGENT_PAIRING\n"
    "relay: wss://relay.nevoflux.app\n"
    f"channel: {CHANNEL}\n"
    "code: A-BCDE-FGHJ-KMNP\n"
)


def test_parse():
    assert pairing.parse_block(BLOCK) == pairing.PairingBlock(
        "wss://relay.nevoflux.app", CHANNEL, "A-BCDE-FGHJ-KMNP")


def test_parse_tolerates_chat_noise():
    text = (
        "Here is the block from my sidebar:\r\n\r\n```\r\n"
        "NEVOFLUX_AGENT_PAIRING  \r\n"
        "  relay:   wss://relay.nevoflux.app/  \r\n"
        f"channel: {CHANNEL}\r\n"
        "code: a bcde fghj kmnp\r\n```\r\n"
    )
    assert pairing.parse_block(text) == pairing.PairingBlock(
        "wss://relay.nevoflux.app", CHANNEL, "A-BCDE-FGHJ-KMNP")


def test_parse_without_header():
    assert pairing.parse_block(BLOCK.split("\n", 1)[1]).channel == CHANNEL


@pytest.mark.parametrize("field", ["relay", "channel", "code"])
def test_parse_missing_field(field):
    text = "\n".join(line for line in BLOCK.splitlines()
                     if not line.startswith(field))
    with pytest.raises(pairing.InvalidPairingBlock) as e:
        pairing.parse_block(text)
    assert f"{field}:" in str(e.value)


@pytest.mark.parametrize("relay",
                         ["http://relay.nevoflux.app",
                          "ws://relay.nevoflux.app", "relay"])
def test_parse_rejects_insecure_relays(relay):
    with pytest.raises(pairing.InvalidPairingBlock):
        pairing.parse_block(BLOCK.replace("wss://relay.nevoflux.app",
                                          relay))


@pytest.mark.parametrize("relay",
                         ["ws://127.0.0.1:8765", "ws://localhost:8765"])
def test_parse_allows_local_test_relays(relay):
    assert pairing.parse_block(BLOCK.replace("wss://relay.nevoflux.app",
                                             relay)).relay == relay


def test_parse_rejects_a_bad_code():
    with pytest.raises(crypto.InvalidPairingCode):
        pairing.parse_block(BLOCK.replace("A-BCDE-FGHJ-KMNP",
                                          "A-BCDE-FGHJ-KMN"))


def test_pair_stores_the_key_not_the_code(tmp_path):
    block = pairing.pair(BLOCK, tmp_path)
    raw = (tmp_path / state.PAIRING_FILE).read_text()
    assert "BCDE" not in raw
    loaded = pairing.load_pairing(tmp_path)
    assert loaded == pairing.Pairing(block.relay, CHANNEL,
                                     crypto.derive_channel_key(block.code,
                                                               CHANNEL))
    assert (base64.b64decode(
        state.read_state(tmp_path, state.PAIRING_FILE)["key"]) ==
            loaded.key)


def test_repairing_keeps_the_account(tmp_path):
    state.write_secret(tmp_path, state.ACCOUNT_FILE,
                       {"base_url": "x", "access_token": "t"})
    pairing.pair(BLOCK, tmp_path)
    pairing.pair(BLOCK.replace(CHANNEL, "other"), tmp_path)
    assert pairing.load_pairing(tmp_path).channel == "other"
    assert state.read_state(tmp_path, state.ACCOUNT_FILE)[
        "access_token"] == "t"


def test_failed_pairing_writes_nothing(tmp_path):
    with pytest.raises(ValueError):
        pairing.pair("code: nope", tmp_path)
    assert not tmp_path.joinpath(state.PAIRING_FILE).exists()


def test_load_pairing_absent(tmp_path):
    assert pairing.load_pairing(tmp_path) is None
