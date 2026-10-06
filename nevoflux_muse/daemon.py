"""Running the bridge: in the foreground, detached, or on demand. POSIX only.

The bridge holds an exclusive flock on bridge.lock for as long as it runs, so at
most one bridge serves a state directory however many `nf ensure`s race to
start one; whoever misses the lock exits. Nothing trusts a pid file: a bridge
is alive exactly when it answers a ping on bridge.sock. A VM rebuild leaves the
socket and lock files behind, but nothing answers the socket and the lock died
with its process.

A detached bridge writes its stdout and stderr to bridge.out (truncated at each
start) and its log to bridge.log, rotated at 1 MiB. The process runs with umask
077: everything it creates is the owner's alone.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, auth, ipc, pairing, state
from .bridge import Bridge, serve

SUPPORTED = sys.platform != "win32"
LOCK_FILE = "bridge.lock"
LOG_FILE = "bridge.log"
OUT_FILE = "bridge.out"
START_TIMEOUT = 10.0

log = logging.getLogger(__name__)


class Unsupported(Exception):
    def __init__(self):
        super().__init__("the bridge needs Linux or macOS (unsupported on Windows)")


class NotReady(Exception):
    pass


class AlreadyRunning(Exception):
    pass


def _require() -> None:
    if not SUPPORTED:
        raise Unsupported()


def alive(d: Path) -> dict | None:
    if not SUPPORTED:
        return None
    try:
        reply = ipc.request(d, {"op": "ping"}, 2.0)
    except ipc.BridgeDown:
        return None
    return reply if reply.get("ok") else None


def ready(d: Path) -> bool:
    return (pairing.load_pairing(d) is not None
            and state.read_state(d, state.ACCOUNT_FILE, auth.ACCOUNT_FIELDS) is not None)


def detach(d: Path) -> None:
    _require()
    state.ensure_dir(d)
    out = d / OUT_FILE
    with open(out, "wb") as f:
        os.chmod(out, 0o600)
        subprocess.Popen(
            [sys.executable, "-m", "nevoflux_muse.cli", "daemon"],
            stdin=subprocess.DEVNULL, stdout=f, stderr=f, start_new_session=True,
            env={**os.environ, "NF_HOME": str(d)}, close_fds=True)


def ensure(d: Path, timeout: float = START_TIMEOUT) -> bool:
    """Make sure a bridge serves d. True if this call started it."""
    _require()
    if alive(d):
        return False
    if not ready(d):
        raise NotReady("pair (`nf pair --from-stdin`) and sign in (`nf auth begin`) first")
    detach(d)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.1)
        if alive(d):
            return True
    raise ipc.BridgeDown(f"the bridge did not start; see {d / OUT_FILE} and {d / LOG_FILE}")


def stop(d: Path, timeout: float = 5.0) -> bool:
    """Ask a running bridge to exit and wait for it. False if none was running."""
    if not SUPPORTED:
        return False
    try:
        ipc.request(d, {"op": "shutdown"}, 2.0)
    except ipc.BridgeDown:
        return False
    deadline = time.monotonic() + timeout
    while alive(d) and time.monotonic() < deadline:
        time.sleep(0.05)
    return True


def _setup_logging(d: Path) -> None:
    handler = logging.handlers.RotatingFileHandler(
        d / LOG_FILE, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    level = logging.getLevelName(os.environ.get("NF_LOG_LEVEL", "INFO").upper())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level if isinstance(level, int) else logging.INFO)


def run_foreground(d: Path) -> int:
    _require()
    import fcntl

    state.ensure_dir(d)
    fd = os.open(d / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("nf: another bridge is already running for this state directory",
                  file=sys.stderr)
            return 1
        os.umask(0o077)  # only once we are the bridge: the refusal path leaves the caller alone
        _setup_logging(d)
        return asyncio.run(_main(d))
    finally:
        os.close(fd)  # releases the lock


async def _main(d: Path) -> int:
    bridge = Bridge(d)
    await bridge.start()
    if bridge.state == "not_ready":
        log.error("not paired or not signed in; the bridge exits")
        return 1
    path = ipc.socket_path(d)
    path.unlink(missing_ok=True)
    server = await serve(bridge, path)
    inode = path.stat().st_ino
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, bridge.request_stop)
    log.info("bridge %s started (pid %s)", __version__, os.getpid())
    try:
        await bridge.wait_stopped()
    finally:
        server.close()
        await server.wait_closed()
        await bridge.stop()
        with contextlib.suppress(FileNotFoundError):
            if path.stat().st_ino == inode:  # still ours: a newer bridge may own the name
                path.unlink()
        log.info("bridge stopped")
    return 0
