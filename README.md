# nevoflux-muse

Connect a Muse agent to your NevoFlux browser — MCP over an E2E-encrypted relay.

The client an AI agent runs (in its own VM) to drive a NevoFlux browser it has
been paired with. The protocol is documented in the head's repository:
[`docs/muse-client-protocol.md`](https://github.com/nevoflux-browser/nevoflux-agent/blob/main/docs/muse-client-protocol.md).

Status: protocol core. `nf pair`, `nf auth begin`, `nf status`, `nf unpair`,
`nf reset` and `nf version` work, and the client can hold an MCP session with a
head; the resident bridge, `nf tools` / `nf call` and the installer come next.

## Commands

Every command is non-interactive and takes `--json`.

```sh
printf '%s\n' '<the block from /pair-agent>' | nf pair --from-stdin
nf auth begin          # prints a URL and a code for the person to approve
nf status              # repeat every 5 s until auth is "ok"; state "ready" when done
```

## State and uninstalling

The client keeps its pairing (the channel key), its account token and any sign-in
in progress in one directory: `$NF_HOME` if set, else `%LOCALAPPDATA%\nevoflux-muse`
on Windows, `~/Library/Application Support/nevoflux-muse` on macOS, and
`$XDG_STATE_HOME/nevoflux-muse` (default `~/.local/state/nevoflux-muse`) elsewhere.

- `nf unpair` forgets the paired browser and keeps the account login, ready to
  pair with another one. The browser still lists the agent until you remove it
  in NevoFlux, but without the key the agent can no longer connect.
- `nf reset` revokes the account token on the server, checks that it no longer
  works, then forgets everything. If it cannot reach the account service it
  keeps the token and exits 1: run it again later. `pip uninstall` does not
  touch this directory, so uninstall with:

```sh
nf reset && pip uninstall nevoflux-muse
```

Do not uninstall after a failed `nf reset`: the token would stay valid on the
server. `nf reset --local-only` deletes it locally regardless.

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
