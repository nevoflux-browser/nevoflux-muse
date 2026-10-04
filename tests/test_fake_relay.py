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


async def test_a_wrong_token_is_refused(relay):
    ws = await join(relay, token="wrong")
    with pytest.raises(websockets.ConnectionClosed) as exc:
        await asyncio.wait_for(ws.recv(), 2)
    assert exc.value.rcvd.code == 4403
