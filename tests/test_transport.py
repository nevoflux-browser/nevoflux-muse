import asyncio

import pytest

from nevoflux_muse import crypto
from nevoflux_muse.testing import FakeRelay
from nevoflux_muse.transport import Peers, RelayLink, RelayRefused, RelayUnreachable

KEY = bytes(range(32))


@pytest.fixture
async def relay():
    r = await FakeRelay().start()
    yield r
    await r.stop()


async def recv(link):
    return await asyncio.wait_for(link.recv(), 2)


async def test_peers_and_sealed_messages(relay):
    a = await RelayLink.connect(relay.url, "c", "tok", KEY)
    assert await recv(a) == Peers(0)
    b = await RelayLink.connect(relay.url, "c", "tok", KEY)
    assert await recv(b) == Peers(1)
    assert await recv(a) == Peers(1)
    await b.send({"k": "frame", "frame": {"x": 1}})
    assert await recv(a) == {"k": "frame", "frame": {"x": 1}}
    await a.close()
    await b.close()


async def test_what_cannot_be_read_is_dropped(relay):
    a = await RelayLink.connect(relay.url, "c", "tok", KEY)
    b = await RelayLink.connect(relay.url, "c", "tok", KEY)
    await recv(a)
    await recv(b)
    await recv(a)
    for junk in ["not json", '{"k":"other"}', '{"k":"peers","n":true}', b"garbage",
                 crypto.seal(bytes(32), {"wrong": "key"})]:
        await b.ws.send(junk)
    await b.send({"ok": 1})
    assert await recv(a) == {"ok": 1}
    await a.close()
    await b.close()


async def test_messages_are_binary(relay):
    a = await RelayLink.connect(relay.url, "c", "tok", KEY)
    b = await RelayLink.connect(relay.url, "c", "tok", KEY)
    await recv(a)
    await recv(b)
    await recv(a)
    await a.send({"x": 1})
    raw = await asyncio.wait_for(b.ws.recv(), 2)
    assert isinstance(raw, bytes) and crypto.open_sealed(KEY, raw) == {"x": 1}
    await a.close()
    await b.close()


async def test_refusals(relay):
    strict = await FakeRelay(expect_token="good").start()
    try:
        with pytest.raises(RelayRefused) as e:
            await RelayLink.connect(strict.url, "c", "bad-token-value", KEY)
        assert e.value.status == 401 and "bad-token-value" not in str(e.value)
    finally:
        await strict.stop()
    with pytest.raises(RelayRefused) as e:
        await RelayLink.connect(relay.url, "", "tok", KEY)
    assert e.value.status == 400
    owner = await RelayLink.connect(relay.url, "c", "owner", KEY)
    with pytest.raises(RelayRefused) as e:
        await RelayLink.connect(relay.url, "c", "someone-else", KEY)
    assert e.value.status == 403
    await owner.close()


async def test_unreachable_hides_the_token():
    with pytest.raises(RelayUnreachable) as e:
        await RelayLink.connect("ws://127.0.0.1:9", "c", "secret-jwt", KEY, open_timeout=2)
    assert "secret-jwt" not in str(e.value)
