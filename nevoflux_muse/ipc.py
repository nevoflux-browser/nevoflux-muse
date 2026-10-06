"""The local socket between `nf` and the bridge.

One request per connection: the client writes one JSON object and a newline,
the bridge answers with one JSON object and a newline and closes. Replies are
{"ok": true, ...} or {"ok": false, "error": {"code", "message", ...}}. Lines
may be large (screenshots travel as base64), up to MAX_LINE.
"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

SOCKET_FILE = "bridge.sock"
MAX_LINE = 64 * 1024 * 1024
CONNECT_WAIT = 15.0
CALL_TIMEOUT = 120.0
MAX_IN_FLIGHT = 8


class BridgeDown(Exception):
    pass


def socket_path(d: Path) -> Path:
    return d / SOCKET_FILE


def error_reply(code: str, message: str, **extra) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, **extra}}


def request(d: Path, msg: dict, timeout: float) -> dict:
    if not hasattr(socket, "AF_UNIX"):
        raise BridgeDown("the bridge needs Linux or macOS (unsupported on Windows)")
    buf = bytearray()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect(str(socket_path(d)))
        except OSError:
            raise BridgeDown("the bridge is not running") from None
        try:
            s.sendall(json.dumps(msg).encode() + b"\n")
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_LINE:
                    raise BridgeDown("the bridge sent an oversized reply")
        except TimeoutError:
            raise BridgeDown("the bridge did not answer in time") from None
        except OSError:
            raise BridgeDown("the connection to the bridge broke") from None
    try:
        reply = json.loads(buf)
    except ValueError:
        raise BridgeDown("the bridge sent an unreadable reply") from None
    if not isinstance(reply, dict):
        raise BridgeDown("the bridge sent an unreadable reply")
    return reply


async def read_request(reader: asyncio.StreamReader) -> object | None:
    try:
        line = await reader.readuntil(b"\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return None
    try:
        return json.loads(line)
    except ValueError:
        return None


async def write_reply(writer: asyncio.StreamWriter, reply: dict) -> None:
    writer.write(json.dumps(reply).encode() + b"\n")
    await writer.drain()
