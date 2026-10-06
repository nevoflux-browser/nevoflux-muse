import asyncio
import json
import socket
import sys

import pytest

from nevoflux_muse import ipc

posix = pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX")


def test_error_reply():
    assert ipc.error_reply("timeout", "too slow", state="connecting") == {
        "ok": False, "error": {"code": "timeout", "message": "too slow", "state": "connecting"}}


def test_socket_path(tmp_path):
    assert ipc.socket_path(tmp_path) == tmp_path / "bridge.sock"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_request_on_windows(tmp_path):
    with pytest.raises(ipc.BridgeDown) as e:
        ipc.request(tmp_path, {"op": "ping"}, 1)
    assert "Windows" in str(e.value)


async def serve(tmp_path, handler):
    async def on_client(reader, writer):
        msg = await ipc.read_request(reader)
        await ipc.write_reply(writer, handler(msg))
        writer.close()
    return await asyncio.start_unix_server(on_client, path=str(ipc.socket_path(tmp_path)),
                                           limit=ipc.MAX_LINE)


@posix
async def test_round_trip(tmp_path):
    server = await serve(tmp_path, lambda msg: {"ok": True, "echo": msg})
    try:
        reply = await asyncio.to_thread(ipc.request, tmp_path, {"op": "ping", "x": [1]}, 5)
        assert reply == {"ok": True, "echo": {"op": "ping", "x": [1]}}
    finally:
        server.close()
        await server.wait_closed()


@posix
async def test_unreadable_request_reads_as_none(tmp_path):
    seen = []
    server = await serve(tmp_path, lambda msg: seen.append(msg) or {"ok": False})

    def send_garbage():
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(str(ipc.socket_path(tmp_path)))
            s.sendall(b"not json\n")
            return s.recv(100)

    try:
        assert json.loads(await asyncio.to_thread(send_garbage)) == {"ok": False}
        assert seen == [None]
    finally:
        server.close()
        await server.wait_closed()


@posix
def test_no_socket_is_bridge_down(tmp_path):
    with pytest.raises(ipc.BridgeDown):
        ipc.request(tmp_path, {"op": "ping"}, 1)


@posix
def test_stale_socket_file_is_bridge_down(tmp_path):
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(ipc.socket_path(tmp_path)))
    s.close()  # the file stays, nobody listens
    with pytest.raises(ipc.BridgeDown):
        ipc.request(tmp_path, {"op": "ping"}, 1)


@posix
async def test_silent_bridge_times_out(tmp_path):
    async def on_client(reader, writer):
        await asyncio.sleep(5)
    server = await asyncio.start_unix_server(on_client, path=str(ipc.socket_path(tmp_path)))
    try:
        with pytest.raises(ipc.BridgeDown) as e:
            await asyncio.to_thread(ipc.request, tmp_path, {"op": "ping"}, 0.3)
        assert "in time" in str(e.value)
    finally:
        server.close()
