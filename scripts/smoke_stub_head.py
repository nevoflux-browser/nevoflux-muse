"""End-to-end check against the reference stub head (dev only, not run in CI).

    python scripts/smoke_stub_head.py --stub ../nevoflux-agent/target/debug/examples/agent_stub_head

Starts a FakeRelay on 127.0.0.1:8765, runs the stub head (built in the
nevoflux-agent repository with `cargo build -p nevoflux-daemon --example
agent_stub_head`) against it with a throwaway pairing, and drives initialize,
tools/list and tools/call through HeadConnection. Without --stub it prints the
environment for starting the stub by hand and waits for it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import subprocess
import sys
import uuid

from mcp import MCPError

from nevoflux_muse import crypto
from nevoflux_muse.pairing import Pairing
from nevoflux_muse.session import HeadConnection
from nevoflux_muse.testing import FakeRelay

PORT = 8765
EXPECTED_TOOLS = ["browser_navigate", "browser_snapshot"]


async def token() -> str:
    return "test"  # the stub head's default NF_STUB_TOKEN; FakeRelay checks no JWT


async def run(stub: str | None) -> int:
    relay = await FakeRelay(port=PORT).start()
    channel = str(uuid.uuid4())
    code = crypto.normalize_code("".join(secrets.choice(crypto.ALPHABET) for _ in range(13)))
    env = {"NF_STUB_RELAY": relay.url, "NF_STUB_CHANNEL": channel, "NF_STUB_CODE": code,
           "NF_STUB_TOKEN": "test"}
    proc = None
    if stub:
        proc = subprocess.Popen(  # noqa: ASYNC220 - dev script, spawn is instant
            [stub], env={**os.environ, **env}
        )
    else:
        print("Start the stub head with:")
        for k, v in env.items():
            print(f"  {k}={v}")
    try:
        key = crypto.derive_channel_key(code, channel)
        async with HeadConnection(Pairing(relay.url, channel, key), token) as conn:
            s = await conn.session(timeout=120 if stub else 900)
            print("initialize:", s.server_info.name, s.server_info.version,
                  s.server_capabilities.experimental)
            tools = sorted(t.name for t in (await s.list_tools()).tools)
            print("tools/list:", tools)
            assert tools == EXPECTED_TOOLS, tools
            result = await s.call_tool("browser_snapshot", {})
            print("tools/call browser_snapshot:", result.model_dump(mode="json", exclude_none=True))
            assert not result.is_error
            try:
                await s.call_tool("bash", {"command": "id"})
                raise AssertionError("bash should be refused")
            except MCPError as e:
                print("tools/call bash:", e.code, e.data)
                assert e.data == {"code": "not_allowed"}
        print("OK")
        return 0
    finally:
        if proc:
            proc.terminate()
            proc.wait(10)
        await relay.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stub", help="path to the built agent_stub_head binary")
    return asyncio.run(run(parser.parse_args().stub))


if __name__ == "__main__":
    sys.exit(main())
