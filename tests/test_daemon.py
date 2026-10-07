import asyncio
import base64
import contextlib
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from nevoflux_muse import daemon, ipc, state
from nevoflux_muse.testing import FakeHead, FakeRelay

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the bridge needs AF_UNIX")

KEY = bytes(range(32))
CHANNEL = "chan-1"


@pytest.fixture
async def world(tmp_path, monkeypatch, account):
    d = tmp_path / "nf"
    monkeypatch.setenv("NF_HOME", str(d))
    relay = await FakeRelay().start()
    account.valid.add("tok-1")
    head = await FakeHead(relay.url, CHANNEL, KEY, token="jwt-for-tok-1").start()
    state.write_secret(d, state.PAIRING_FILE, {
        "relay": relay.url, "channel": CHANNEL, "key": base64.b64encode(KEY).decode()})
    state.write_secret(d, state.ACCOUNT_FILE,
                       {"base_url": account.url, "access_token": "tok-1"})
    yield SimpleNamespace(d=d, relay=relay, head=head, account=account)
    await asyncio.to_thread(daemon.stop, d)
    await head.stop()
    await relay.stop()


def my_bridges(d):
    out = subprocess.run(["pgrep", "-f", "nevoflux_muse.cli daemon"], capture_output=True,
                         text=True, check=False).stdout.split()
    mine = []
    for p in out:
        try:
            with open(f"/proc/{p}/environ", "rb") as f:
                env = f.read().split(b"\0")
        except OSError:  # it exited since pgrep saw it
            continue
        if f"NF_HOME={d}".encode() in env:
            mine.append(p)
    return mine


async def bridge_state(d):
    reply = await asyncio.to_thread(ipc.request, d, {"op": "status"}, 5)
    return reply["state"]


async def wait_state(d, wanted, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await bridge_state(d) == wanted:
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"bridge never reached {wanted}")


async def test_ensure_starts_once(world):
    assert await asyncio.to_thread(daemon.ensure, world.d) is True
    assert await asyncio.to_thread(daemon.ensure, world.d) is False
    await wait_state(world.d, "connected")
    assert (world.d / "bridge.sock").stat().st_mode & 0o077 == 0


async def test_ensure_ignores_a_stale_socket(world):
    state.ensure_dir(world.d)
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(ipc.socket_path(world.d)))
    s.close()  # a socket file nobody listens on, as after a VM rebuild
    assert await asyncio.to_thread(daemon.ensure, world.d) is True
    assert daemon.alive(world.d)


async def test_concurrent_ensures_leave_one_bridge(world):
    results = []
    threads = [threading.Thread(target=lambda: results.append(daemon.ensure(world.d)))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert len(results) == 2
    pid = daemon.alive(world.d)["pid"]
    await asyncio.sleep(1.5)  # a loser exits as soon as it misses the lock
    assert daemon.alive(world.d)["pid"] == pid
    mine = await asyncio.to_thread(my_bridges, world.d)
    assert len(mine) == 1


async def test_ensure_restarts_after_kill(world):
    await asyncio.to_thread(daemon.ensure, world.d)
    pid = daemon.alive(world.d)["pid"]
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while daemon.alive(world.d) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert await asyncio.to_thread(daemon.ensure, world.d) is True
    assert daemon.alive(world.d)["pid"] != pid


async def test_stop(world):
    await asyncio.to_thread(daemon.ensure, world.d)
    assert await asyncio.to_thread(daemon.stop, world.d) is True
    assert daemon.alive(world.d) is None
    assert not ipc.socket_path(world.d).exists()
    assert await asyncio.to_thread(daemon.stop, world.d) is False


async def test_ensure_needs_pairing_and_sign_in(world):
    (world.d / state.ACCOUNT_FILE).unlink()
    with pytest.raises(daemon.NotReady):
        await asyncio.to_thread(daemon.ensure, world.d)


async def test_a_second_foreground_bridge_refuses(world):
    await asyncio.to_thread(daemon.ensure, world.d)
    assert await asyncio.to_thread(daemon.run_foreground, world.d) == 1


async def test_the_log_is_private_and_holds_no_secrets(world):
    await asyncio.to_thread(daemon.ensure, world.d)
    await wait_state(world.d, "connected")
    await asyncio.to_thread(ipc.request, world.d,
                            {"op": "call", "tool": "browser_snapshot", "args": {}}, 30)
    log = world.d / daemon.LOG_FILE
    assert log.stat().st_mode & 0o077 == 0
    text = log.read_text()
    assert "call 'browser_snapshot' -> ok" in text
    for secret in ("tok-1", "jwt-for-tok-1", base64.b64encode(KEY).decode()):
        assert secret not in text


async def test_cli_end_to_end(world, capsys):
    import json

    from nevoflux_muse.cli import main

    async def nf(*argv):
        code = await asyncio.to_thread(main, [*argv, "--json"])
        return code, json.loads(capsys.readouterr().out)

    deadline = time.monotonic() + 20
    while True:
        code, out = await nf("status")
        if out["state"] == "connected" or time.monotonic() > deadline:
            break
        await asyncio.sleep(0.3)
    assert out["state"] == "connected" and out["head"]["name"] == "nevoflux-head"
    code, tools = await nf("tools")
    assert code == 0 and [t["name"] for t in tools] == ["browser_snapshot", "browser_navigate"]
    code, result = await nf("call", "browser_snapshot")
    assert code == 0 and result["content"][0]["text"] == "fake browser_snapshot"
    code, err = await nf("call", "bash", "command=id")
    assert code == 1 and err["code"] == "mcp_error"
    code, out = await nf("reset")
    assert code == 0 and out["revoked"] is True
    assert not ipc.socket_path(world.d).exists() and daemon.alive(world.d) is None


async def until_gone(d, timeout=5):
    deadline = time.monotonic() + timeout
    while await asyncio.to_thread(my_bridges, d) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    return await asyncio.to_thread(my_bridges, d)


async def test_stop_while_a_call_hangs(world):
    await asyncio.to_thread(daemon.ensure, world.d)
    await wait_state(world.d, "connected")
    ended = []

    def hang():
        try:
            ended.append(ipc.request(world.d, {"op": "call", "tool": "hang", "args": {}}, 30))
        except ipc.BridgeDown as e:
            ended.append(e)

    caller = threading.Thread(target=hang)
    caller.start()
    deadline = time.monotonic() + 10
    while not any((m.get("params") or {}).get("name") == "hang" for m in world.head.requests):
        assert time.monotonic() < deadline, "the call never reached the head"
        await asyncio.sleep(0.05)
    started = time.monotonic()
    assert await asyncio.to_thread(daemon.stop, world.d) is True
    assert time.monotonic() - started < 3.5
    assert daemon.alive(world.d) is None
    assert not ipc.socket_path(world.d).exists()
    await asyncio.to_thread(caller.join, 5)
    assert ended, "the hanging call never ended"
    if isinstance(ended[0], dict):
        assert ended[0]["error"]["code"] == "connection_lost"
    assert await until_gone(world.d) == []


async def test_sigterm_stops_the_bridge(world):
    await asyncio.to_thread(daemon.ensure, world.d)
    os.kill(daemon.alive(world.d)["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 3
    while ipc.socket_path(world.d).exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not ipc.socket_path(world.d).exists()
    assert daemon.alive(world.d) is None
    assert await until_gone(world.d) == []


async def test_held_keeps_any_bridge_from_starting(world):
    await asyncio.to_thread(daemon.ensure, world.d)

    def inside():
        with daemon.held(world.d):
            assert daemon.alive(world.d) is None
            with pytest.raises(ipc.BridgeDown):
                daemon.ensure(world.d, timeout=1)
            deadline = time.monotonic() + 5  # the one ensure started exits on the lock
            while my_bridges(world.d) and time.monotonic() < deadline:
                time.sleep(0.1)
            assert my_bridges(world.d) == []

    await asyncio.to_thread(inside)
    assert await asyncio.to_thread(daemon.ensure, world.d) is True


async def test_reset_with_a_running_bridge_leaves_none(world, capsys):
    from nevoflux_muse.cli import main

    await asyncio.to_thread(daemon.ensure, world.d)
    assert await asyncio.to_thread(main, ["reset", "--local-only", "--json"]) == 0
    capsys.readouterr()
    assert not ipc.socket_path(world.d).exists() and daemon.alive(world.d) is None
    assert await until_gone(world.d) == []


async def test_the_bridge_ignores_the_callers_directory(world, tmp_path, monkeypatch):
    agent = tmp_path / "agent"
    agent.mkdir()
    for name in ("random", "secrets"):
        (agent / f"{name}.py").write_text("raise ImportError('shadowed')\n")
    monkeypatch.chdir(agent)
    assert await asyncio.to_thread(daemon.ensure, world.d) is True
    await wait_state(world.d, "connected")


async def test_ensure_replaces_a_bridge_of_another_version(world, monkeypatch):
    await asyncio.to_thread(daemon.ensure, world.d)
    pid = daemon.alive(world.d)["pid"]
    monkeypatch.setattr(daemon, "__version__", "99.0.0")  # as after `pip install -U`
    assert await asyncio.to_thread(daemon.ensure, world.d) is True
    assert daemon.alive(world.d)["pid"] != pid


async def test_foreground_not_ready_exits_3(tmp_path, monkeypatch):
    d = tmp_path / "empty"
    monkeypatch.setenv("NF_HOME", str(d))
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "nevoflux_muse.cli", "daemon", cwd=str(tmp_path))
    assert await asyncio.wait_for(proc.wait(), 20) == daemon.EXIT_NOT_READY


async def test_ensure_uses_systemd_when_supervised(world, monkeypatch):
    calls = []
    monkeypatch.setattr(daemon, "run_systemctl",
                        lambda *a: calls.append(a) or daemon.detach(world.d) or 0)
    state.write_supervision(world.d, "systemd-user")
    assert await asyncio.to_thread(daemon.ensure, world.d) is True
    assert calls == [("start", daemon.UNIT_NAME)]


async def test_lock_retry_absorbs_a_brief_holder(world):
    import fcntl
    state.ensure_dir(world.d)
    fd = os.open(world.d / daemon.LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    # held past a detached interpreter's start-up, but under LOCK_RETRY
    threading.Timer(0.6, os.close, [fd]).start()
    assert await asyncio.to_thread(daemon.ensure, world.d) is True


async def test_stop_follows_a_replacement_bridge(world, monkeypatch):
    await asyncio.to_thread(daemon.ensure, world.d)
    first = daemon.alive(world.d)["pid"]
    real_request = ipc.request
    shutdowns = []

    def swallow_shutdown(d, msg, timeout):
        if msg.get("op") == "shutdown":
            shutdowns.append(1)
            if len(shutdowns) == 1:  # the first bridge ignores it...
                os.kill(first, signal.SIGKILL)  # ...dies, and a parallel ensure starts another
                daemon.detach(d)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:  # stop must meet the replacement
                    try:
                        reply = real_request(d, {"op": "ping"}, 2.0)
                    except ipc.BridgeDown:
                        reply = {}
                    if reply.get("ok") and reply.get("pid") != first:
                        break
                    time.sleep(0.05)
                return {"ok": True}
        return real_request(d, msg, timeout)

    monkeypatch.setattr(ipc, "request", swallow_shutdown)
    try:
        assert await asyncio.to_thread(daemon.stop, world.d, 1.0) is True
        monkeypatch.setattr(ipc, "request", real_request)
        assert len(shutdowns) == 2  # the replacement got its own shutdown
        assert daemon.alive(world.d) is None
        assert await until_gone(world.d) == []
    finally:
        monkeypatch.setattr(ipc, "request", real_request)
        for pid in await asyncio.to_thread(my_bridges, world.d):
            with contextlib.suppress(OSError):
                os.kill(int(pid), signal.SIGKILL)
