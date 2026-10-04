# nevoflux-muse

Connect a Muse agent to your NevoFlux browser — MCP over an E2E-encrypted relay.

The client an AI agent runs (in its own VM) to drive a NevoFlux browser it has
been paired with. The protocol is documented in the head's repository:
[`docs/muse-client-protocol.md`](https://github.com/nevoflux-browser/nevoflux-agent/blob/main/docs/muse-client-protocol.md).

Status: skeleton. `nf version`, `nf unpair` and `nf reset` work; pairing, the
relay bridge and the rest of `nf` arrive with the reference implementation.

## State and uninstalling

The client keeps its pairing (the channel key) and account tokens in one
directory: `$NF_HOME` if set, else `%LOCALAPPDATA%\nevoflux-muse` on Windows,
`~/Library/Application Support/nevoflux-muse` on macOS, and
`$XDG_STATE_HOME/nevoflux-muse` (default `~/.local/state/nevoflux-muse`)
elsewhere.

- `nf unpair` forgets the paired browser and keeps the account login, ready to
  pair with another one. The browser still lists the agent until you remove it
  in NevoFlux, but without the key the agent can no longer connect.
- `nf reset` forgets everything. `pip uninstall` does not touch this directory,
  so uninstall with:

```sh
nf reset
pip uninstall nevoflux-muse
```

## Development

```sh
python -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install --no-deps -e . && .venv/bin/pip install pytest pytest-asyncio ruff
.venv/bin/pytest -q
python scripts/check_fixtures.py          # fixtures match the pinned head commit
```

Tests use `nevoflux_muse.testing.FakeRelay` as the relay test double. It differs from the
production relay in a few documented ways (no per-channel capacity, no `ping`/`pong`, no
`/presence` probe, no JWT verification); see its module docstring.

`fixtures/muse/` is a byte-exact copy of the head's
`crates/daemon/tests/fixtures/muse/` at the commit in `fixtures/SOURCE.json`.
Never edit it by hand: change the head, then re-copy and re-pin.

Never commit keys, tokens, pairing codes or account details. CI scans for them.

## License

MIT
