# nevoflux-muse

Connect a Muse agent to your NevoFlux browser — MCP over an E2E-encrypted relay.

The client an AI agent runs (in its own VM) to drive a NevoFlux browser it has
been paired with. The protocol is documented in the head's repository:
[`docs/muse-client-protocol.md`](https://github.com/nevoflux-browser/nevoflux-agent/blob/main/docs/muse-client-protocol.md).

Status: protocol core, resident bridge and installer. `nf pair`, `nf auth begin`,
`nf status`, `nf tools`, `nf call`, `nf ensure`, `nf daemon`, `nf setup`, `nf unpair`,
`nf reset`, `nf uninstall` and `nf version` work.

## Install

On a Linux VM, an agent installs the client by following [`INSTALL.md`](INSTALL.md),
which gives every step, its expected output and what to tell the person. In short:

```sh
git clone --depth 1 -b v0.2.0 https://github.com/nevoflux-browser/nevoflux-muse ~/.local/share/nevoflux-muse/src
~/.local/share/nevoflux-muse/src/setup.sh
```

`setup.sh` builds a private environment, links `~/.local/bin/nf`, and runs `nf setup`,
which keeps the bridge running with a systemd user unit or, without one, a watchdog
(`ensure.sh`, run every 5 minutes).

## Commands

Every command is non-interactive and takes `--json`.

```sh
printf '%s\n' '<the block from /pair-agent>' | nf pair --from-stdin
nf auth begin          # prints a URL and a code for the person to approve
nf status              # repeat every 5 s until auth is "ok"; state "connected" when done
```

Once paired and signed in, a resident bridge keeps the connection to the browser
open; `nf status`, `nf tools` and `nf call` start it when it is not running.

```sh
nf status                          # "connected" when the bridge reaches the browser
nf tools                           # the browser tools on offer
nf call browser_navigate url=https://example.com
printf '%s' '{"text": "..."}' | nf call browser_type --args-stdin
nf ensure                          # start the bridge if it is not running (watchdogs)
```

`nf setup` chooses how the bridge is kept running (`setup.sh` runs it); `nf uninstall`
revokes the sign-in, stops the bridge and removes the installation.

The bridge needs Linux or macOS. On Windows `nf daemon`, `ensure`, `tools`, `call` and
`setup` exit 1 with "unsupported on Windows", and `nf status` stops at `ready`.

The bridge logs to `bridge.log` in the state directory (tool names, outcomes and timings;
never arguments or results).

## State and uninstalling

The client keeps its pairing (the channel key), its account token, any sign-in
in progress and the bridge's files (`bridge.sock`, `bridge.lock`, `bridge.log*`,
`bridge.out`) in one directory: `$NF_HOME` if set, else `%LOCALAPPDATA%\nevoflux-muse`
on Windows, `~/Library/Application Support/nevoflux-muse` on macOS, and
`$XDG_STATE_HOME/nevoflux-muse` (default `~/.local/state/nevoflux-muse`) elsewhere.

- `nf unpair` forgets the paired browser and keeps the account login, ready to
  pair with another one. The browser still lists the agent until you remove it
  in NevoFlux, but without the key the agent can no longer connect.
- `nf reset` stops the bridge, revokes the account token on the server, checks that
  it no longer works, then forgets everything. If it cannot reach the account
  service it keeps the token and exits 1: run it again later. Use it to forget the
  state without uninstalling.
- `nf uninstall` does the same revocation, then removes the installation (the install
  root and the `~/.local/bin/nf` link) and prints what is left for the person to do:
  remove the agent in NevoFlux and delete the watchdog task.

```sh
nf uninstall
```

Do not delete files by hand after a failed `nf uninstall` or `nf reset`: the token would
stay valid on the server. `nf reset --local-only` deletes it locally regardless.

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
