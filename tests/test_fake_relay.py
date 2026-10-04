import asyncio
import json

import pytest
import websockets

from nevoflux_muse.testing.fake_relay import FakeRelay

TOKEN = "tok"


@pytest.fixture
async def relay():
    r = await FakeRelay(expect_token=TOKEN).start()
    yield r
    await r.stop()


async def join(relay, channel="c1", token=TOKEN):
    return await websockets.connect(f"{relay.url}/?c={channel}&t={token}")


async def next_peers(ws) -> int:
    while True:
        msg = await asyncio.wait_for(ws.recv(), 2)
        if isinstance(msg, str):
            data = json.loads(msg)
            if data.get("k") == "peers":
                return data["n"]


async def test_a_lone_socket_hears_it_is_alone(relay):
    a = await join(relay)
    assert await next_peers(a) == 0
    await a.close()


async def test_counts_never_include_the_listener(relay):
    a = await join(relay)
    assert await next_peers(a) == 0
    b = await join(relay)
    assert await next_peers(b) == 1, "the newcomer sees one other"
    assert await next_peers(a) == 1, "the one already there sees one other"
    await b.close()
    assert await next_peers(a) == 0, "the survivor is alone again"
    await a.close()


async def test_messages_reach_others_untouched(relay):
    a = await join(relay)
    await next_peers(a)
    b = await join(relay)
    await next_peers(b)
    await next_peers(a)
    await a.send(b"\x00\x01sealed")
    assert await asyncio.wait_for(b.recv(), 2) == b"\x00\x01sealed"
    await a.close()
    await b.close()


async def test_sending_into_an_empty_channel_says_so(relay):
    a = await join(relay)
    await next_peers(a)
    await a.send(b"anyone?")
    assert await next_peers(a) == 0
    await a.close()


async def test_channels_are_isolated(relay):
    a = await join(relay, "c1")
    await next_peers(a)
    b = await join(relay, "c2")
    assert await next_peers(b) == 0
    await a.close()
    await b.close()


async def refused(relay, query: str) -> int:
    with pytest.raises(websockets.InvalidStatus) as exc:
        await websockets.connect(f"{relay.url}/?{query}")
    return exc.value.response.status_code


async def test_a_wrong_token_is_refused_with_401(relay):
    assert await refused(relay, "c=c1&t=wrong") == 401


async def test_a_missing_or_empty_token_is_refused_with_401(relay):
    assert await refused(relay, "c=c1") == 401
    assert await refused(relay, "c=c1&t=") == 401


async def test_empty_token_is_refused_even_when_any_token_is_accepted():
    r = await FakeRelay().start()
    try:
        assert await refused(r, "c=c1&t=") == 401
        ws = await websockets.connect(f"{r.url}/?c=c1&t=anything")
        assert await next_peers(ws) == 0
        await ws.close()
    finally:
        await r.stop()


async def test_a_missing_channel_is_refused_with_400(relay):
    assert await refused(relay, f"t={TOKEN}") == 400


async def test_another_account_on_an_owned_channel_gets_403():
    r = await FakeRelay().start()
    try:
        a = await websockets.connect(f"{r.url}/?c=c1&t=alice")
        assert await next_peers(a) == 0
        assert await refused(r, "c=c1&t=bob") == 403
        b = await websockets.connect(f"{r.url}/?c=c1&t=alice")
        assert await next_peers(b) == 1
        await a.close()
        await b.close()
    finally:
        await r.stop()
