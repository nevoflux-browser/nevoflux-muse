"""CI only: drive an installed nf end to end (acceptance criteria 1 and 4).

Run with the installed venv's Python after setup.sh, with HOME, NF_HOME and
NF_INSTALL_ROOT pointing at a scratch home: starts a FakeRelay, a FakeHead and the
account double in this process, then runs ~/.local/bin/nf pair, auth begin, status
(until connected), call and uninstall, and checks what uninstall left behind.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

from nevoflux_muse import crypto, ipc, state
from nevoflux_muse.testing import AccountDouble, FakeHead, FakeRelay

NF = str(Path.home() / ".local" / "bin" / "nf")
ROOT = Path(os.environ["NF_INSTALL_ROOT"])
CHANNEL = "chan-e2e"
CODE = "X-ABCD-EFGH-JKMN"


def nf(*argv: str, stdin: str | None = None) -> tuple[int, dict]:
    proc = subprocess.run([NF, *argv, "--json"], input=stdin, capture_output=True, text=True, check=False,
                          timeout=180)
    try:
        out = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except ValueError:
        out = {"raw": proc.stdout, "stderr": proc.stderr}
    return proc.returncode, out


def cleanup() -> None:
    """Best effort after a failure: leave no bridge running for this NF_HOME."""
    try:
        d = state.state_dir()
        if ipc.socket_path(d).exists() and os.path.lexists(NF):
            subprocess.run([NF, "uninstall", "--json"], capture_output=True, timeout=120, check=False)
        if ipc.socket_path(d).exists() and os.path.lexists(NF):
            subprocess.run([NF, "reset", "--local-only", "--json"], capture_output=True,
                           timeout=120, check=False)
    except Exception as e:  # noqa: BLE001
        print(f"cleanup: {e}")
    subprocess.run(["pkill", "-f", "nevoflux_muse.cli daemon"], capture_output=True,
                   check=False)


async def run() -> None:
    account = AccountDouble().start()
    os.environ["NEVOFLUX_ACCOUNT_URL"] = account.url
    relay = await FakeRelay().start()
    key = crypto.derive_channel_key(CODE, CHANNEL)
    head = await FakeHead(relay.url, CHANNEL, key, token="jwt-for-tok-1").start()
    block = f"NEVOFLUX_AGENT_PAIRING\nrelay: {relay.url}\nchannel: {CHANNEL}\ncode: {CODE}\n"
    try:
        code, out = await asyncio.to_thread(nf, "pair", "--from-stdin", stdin=block)
        assert code == 0, out
        code, out = await asyncio.to_thread(nf, "auth", "begin")
        assert code == 0 and out["user_code"], out
        account.polls = ["approve"]
        deadline = time.monotonic() + 90
        while True:
            code, out = await asyncio.to_thread(nf, "status")
            if out.get("state") == "connected":
                break
            assert time.monotonic() < deadline, f"never connected: {out}"
            await asyncio.sleep(2)
        assert out["supervision"] == "detached+watchdog", out
        code, out = await asyncio.to_thread(nf, "call", "browser_snapshot")
        assert code == 0 and out["content"][0]["text"] == "fake browser_snapshot", out
        code, out = await asyncio.to_thread(nf, "uninstall")
        assert code == 0 and out["ok"], out
        assert account.valid == set(), "the token is still valid"
        assert not ROOT.exists(), "the install root is still there"
        assert not os.path.lexists(NF), "~/.local/bin/nf is still there"
        d = state.state_dir()
        assert not ipc.socket_path(d).exists(), "a bridge socket is left"
        print("install e2e OK")
    except BaseException:
        await asyncio.to_thread(cleanup)
        raise
    finally:
        await head.stop()
        await relay.stop()
        account.stop()


if __name__ == "__main__":
    asyncio.run(run())
