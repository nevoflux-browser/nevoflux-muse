"""Running the bridge: in the foreground, detached, or on demand. POSIX only.

The bridge holds an exclusive flock on bridge.lock for as long as it runs, so at
most one bridge serves a state directory however many `nf ensure`s race to
start one; whoever misses the lock exits. Nothing trusts a pid file: a bridge
is alive exactly when it answers a ping on bridge.sock. A VM rebuild leaves the
socket and lock files behind, but nothing answers the socket and the lock died
with its process.

A detached bridge writes its stdout and stderr to bridge.out (truncated at each
start) and its log to bridge.log, rotated at 1 MiB. The process runs with umask
077: everything it creates is the owner's alone. It runs in the state directory,
so nothing in the caller's working directory can shadow a module it imports.

Under systemd-user supervision (`nf setup`) the unit starts the bridge; `ensure` asks
systemd instead of detaching, and a bridge that is not ready exits 3, which the unit
does not restart.
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
EXIT_NOT_READY = 3
UNIT_NAME = "nevoflux-muse.service"
LOCK_RETRY = 1.0

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
            env={**os.environ, "NF_HOME": str(d)}, close_fds=True, cwd=str(d))


def run_systemctl(*args: str) -> int:
    """`systemctl --user …`; its exit code, 127 when it cannot run at all."""
    try:
        return subprocess.run(["systemctl", "--user", *args], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=30, check=False).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 127


_UNIT = """[Unit]
Description=NevoFlux Muse bridge

[Service]
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
RestartPreventExitStatus={not_ready}
{environment}
[Install]
WantedBy=default.target
"""


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


def _quote(text: str) -> str:
    """One double-quoted systemd word: spaces are safe, and `%` is not a specifier."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def unit_text(d: Path) -> str:
    """The user unit that keeps the bridge running, started from this interpreter."""
    exec_start = f"{_quote(sys.executable)} -m nevoflux_muse.cli daemon"
    environment = ""
    if os.environ.get("NF_HOME"):
        environment = "Environment=" + _quote("NF_HOME=" + str(d)) + "\n"
    return _UNIT.format(exec_start=exec_start, not_ready=EXIT_NOT_READY, environment=environment)


def setup(d: Path, *, service: bool = True) -> dict:
    """Choose how the bridge is kept running, record it, and (re)start the bridge if ready.

    The unit is enabled, not started: setup.sh runs before pairing, and the bridge starts
    once `ensure` finds pairing and sign-in complete. Re-running it (after an upgrade)
    restarts a running bridge so the new code takes over."""
    _require()
    unit = None
    if service and run_systemctl("show-environment") == 0:
        mode, unit = "systemd-user", unit_path()
        try:
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text(unit_text(d), encoding="utf-8")
        except OSError as e:
            raise ipc.BridgeDown(f"cannot write {unit}: {e.strerror}") from e
        if run_systemctl("daemon-reload") != 0 or run_systemctl("enable", UNIT_NAME) != 0:
            raise ipc.BridgeDown(f"systemctl --user could not enable {UNIT_NAME}")
    else:
        mode = "detached+watchdog"
        if unit_path().exists():  # a unit left by an earlier run must not start a second bridge
            remove_unit()
    state.write_supervision(d, mode)
    if alive(d):
        stop(d)
    started = ensure(d) if ready(d) else False
    return {"supervision": mode, "started": started, "unit": str(unit) if unit else None}


def remove_unit() -> None:
    run_systemctl("disable", "--now", UNIT_NAME)
    with contextlib.suppress(FileNotFoundError):
        unit_path().unlink()
    run_systemctl("daemon-reload")


def ensure(d: Path, timeout: float = START_TIMEOUT) -> bool:
    """Make sure a bridge of this version serves d. True if this call started it.

    A bridge of another version (still running after `pip install -U`) is replaced."""
    _require()
    running = alive(d)
    if running and running.get("version") == __version__:
        return False
    if not ready(d):
        raise NotReady("pair (`nf pair --from-stdin`) and sign in (`nf auth begin`) first")
    if running:
        stop(d)
    if state.supervision(d) == "systemd-user":
        if run_systemctl("start", UNIT_NAME) != 0:
            raise ipc.BridgeDown(f"systemctl --user start {UNIT_NAME} failed; "
                                 f"see `journalctl --user -u {UNIT_NAME}`")
    else:
        detach(d)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.1)
        if alive(d):
            return True
    raise ipc.BridgeDown(f"the bridge did not start; see {d / OUT_FILE} and {d / LOG_FILE}")


def stop(d: Path, timeout: float = 5.0) -> bool:
    """Ask a running bridge to exit and wait until it is gone. False if none was running.

    One that has not finished within timeout gets SIGTERM, then SIGKILL, and only while
    the same pid still answers: if a newer bridge answers instead (a parallel ensure),
    that one is stopped next. If it still cannot be seen gone, BridgeDown: the caller
    must not delete its files."""
    if not SUPPORTED:
        return False
    running = alive(d)
    if running is None:
        return False
    deadline = time.monotonic() + 3 * (timeout + 3.0)
    while True:
        pid = running.get("pid")
        with contextlib.suppress(ipc.BridgeDown):
            ipc.request(d, {"op": "shutdown"}, 2.0)
        if _wait_gone(d, timeout):
            return True
        replaced = None
        for sig, wait in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 1.0)):
            now = alive(d)
            if now is not None and now.get("pid") != pid:
                replaced = now
                break
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                with contextlib.suppress(OSError):
                    os.kill(pid, sig)
            if _wait_gone(d, wait):
                return True
        if replaced is None or time.monotonic() >= deadline:
            raise ipc.BridgeDown("the bridge did not stop")
        running = replaced


def _wait_gone(d: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _still_running(d):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


@contextlib.contextmanager
def held(d: Path, timeout: float = 10.0):
    """Stop any bridge and keep a new one from starting until the block ends.

    `nf reset` and `nf unpair` run inside this: a bridge some parallel `nf ensure`
    starts meanwhile misses the lock and exits. A no-op where there is no bridge."""
    if not SUPPORTED or not d.is_dir():
        yield
        return
    import fcntl

    fd = os.open(d / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            stop(d, timeout=max(0.5, min(5.0, left - 3.0)))
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:  # a bridge got it first, or is still starting
                if time.monotonic() >= deadline:
                    raise ipc.BridgeDown("the bridge did not stop") from None
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)  # releases the lock


def _still_running(d: Path) -> bool:
    """Answering, or not yet done tidying up: the lock is released only as the process
    finishes (after it removed its socket), and a restart straight after must not race it.
    A killed bridge leaves its socket file behind but not the lock."""
    import fcntl

    if alive(d):
        return True
    try:
        fd = os.open(d / LOCK_FILE, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


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
        until = time.monotonic() + LOCK_RETRY
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:  # a probe holds it for an instant; a bridge keeps it
                if time.monotonic() >= until:
                    print("nf: another bridge is already running for this state directory",
                          file=sys.stderr)
                    return 1
                time.sleep(0.05)
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
        return EXIT_NOT_READY
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
        try:
            await bridge.stop()  # first: calls in flight then end with connection_lost
        finally:
            server.close()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(server.wait_closed(), 2)
            with contextlib.suppress(FileNotFoundError):
                if path.stat().st_ino == inode:  # still ours: a newer bridge may own the name
                    path.unlink()
            log.info("bridge stopped")
    return 0
