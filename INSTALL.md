# Installing nevoflux-muse (for agents)

This file is written for an AI agent that installs and runs `nf`, the client for the
person's NevoFlux browser, on its own Linux VM, without a terminal prompt. Do the steps in
order. Each step gives the exact command, what success looks like, what to do on failure,
and where the person must be told something ("Tell the person: ...").

Conventions:

- Every `nf` command takes `--json`. Use it and read the fields named below.
- Exit code 0 is success, 1 is failure, 2 is a usage error.
- `nf` is at `~/.local/bin/nf`. Use that full path if `~/.local/bin` is not on `PATH`.
- Never put a secret (pairing block, tokens) in a command-line argument or in your own
  logs. The pairing block goes through stdin only.

## 1. Pre-flight

```sh
python3 --version
git --version
curl --version || wget --version
```

- `git` is required. `curl` or `wget` is required.
- Python 3.10 or newer is used if present. If it is missing or older, the installer
  downloads a pinned `uv` (checksum-verified) and builds the environment with its
  Python 3.12. You do not need to install Python yourself.
- If `git` or both downloaders are missing, install them with the VM's package manager.
  If you cannot, stop.

Tell the person (only if you must stop): "I cannot install the NevoFlux connection because
this machine lacks `git` (or `curl`/`wget`) and I cannot install it."

Optional: see what the installer would do without changing anything. This needs the clone
from step 2.

```sh
~/.local/share/nevoflux-muse/src/setup.sh --dry-run
```

It prints `setup.sh would:` and a list of actions, and exits 0.

## 2. Install

```sh
git clone --depth 1 -b v0.2.0 https://github.com/nevoflux-browser/nevoflux-muse ~/.local/share/nevoflux-muse/src
~/.local/share/nevoflux-muse/src/setup.sh
```

Expected: `setup.sh` ends with one JSON line from `nf setup` followed by a text block:

```text
{"supervision": "systemd-user", "started": false, "unit": "/home/<you>/.config/systemd/user/nevoflux-muse.service"}
Installed nevoflux-muse v0.2.0 into /home/<you>/.local/share/nevoflux-muse.
Next (INSTALL.md): ...
```

- `supervision` is `systemd-user` (a systemd user unit keeps the bridge running; `unit` is
  its path) or `detached+watchdog` (no usable systemd user session; the watchdog in step 3
  keeps it running; `unit` is `null`). Either is fine. `started` is `false` until pairing
  and sign-in are done.
- Check: `~/.local/bin/nf version --json` prints `{"client": "0.2.0", "protocol": {...}}`.

If it fails:

- `setup.sh` exits non-zero and prints a one-line reason to stderr, or logs it to
  `~/.local/share/nevoflux-muse/install.log`. Read the last lines of that file. The build
  detail is in `~/.local/share/nevoflux-muse/pip.out`.
- `another setup.sh or ensure.sh is running`: wait a minute and run it again.
- `~/.local/bin/nf exists and is not ours`: another program owns that path. Move it
  aside and run `setup.sh` again.
- No network or a blocked download: fix the network and run `setup.sh` again. It is
  safe to re-run.
- `git clone` says the destination exists: the install is already there. Skip the clone and
  run `setup.sh` again.
- To skip the systemd unit and rely on the watchdog only: `setup.sh --no-service`.

Tell the person: nothing yet, unless the install failed and you cannot fix it: "I could not
install the NevoFlux connection: <the reason from install.log>."

## 3. Register the watchdog

Register a Muse native scheduled task that runs every 5 minutes (it may be set to every
minute) with exactly this command:

```sh
sh "$HOME/.local/share/nevoflux-muse/ensure.sh"
```

Use the absolute form of the path if your scheduler does not expand `$HOME`. The watchdog
rebuilds the environment if the system Python changed, then starts the bridge if it is not
running. It is harmless before pairing: it exits 0 and logs `not_ready`.

Check by running it once by hand:

```sh
sh "$HOME/.local/share/nevoflux-muse/ensure.sh"; echo "exit $?"
tail -n 3 ~/.local/share/nevoflux-muse/install.log
```

Expected before pairing: `exit 0` and a log line like
`nf ensure: {"running": false, "reason": "not_ready"}`. After pairing and sign-in:
`nf ensure: {"running": true, "started": ...}`.

If it fails: exit 1 and a log line saying why. Read `install.log`, fix, and run it again.
If your scheduler cannot be configured, tell the person.

Tell the person (only if you cannot register the task): "I could not set up the periodic
check that keeps the NevoFlux connection alive. The connection works, but it may stop
after a restart until I run it again."

## 4. Pair

The person runs `/pair-agent` in NevoFlux and gives you the whole text block. Pipe it in.
Do not pass it as an argument.

Tell the person: "Please run `/pair-agent` in NevoFlux and paste me the whole text block it
shows."

```sh
printf '%s\n' '<the whole pairing block>' | ~/.local/bin/nf pair --from-stdin --json
```

Expected: exit 0 and `{"paired": true, "relay": "...", "channel": "..."}`.

If it fails: exit 1 and `{"error": "..."}`. `nf pair` only parses the block and derives the
key locally, so it fails only when the block is incomplete or malformed. It does not check
whether the code is expired or already used: a stale or used code is accepted here and shows
up later as `head_offline` or `relay_refused` in step 6. In that case also ask the person for
a fresh block from `/pair-agent` and repeat this step.

Tell the person (on failure): "That pairing block did not work (<the error>). Please run
`/pair-agent` again and send me the new block."

## 5. Authorize

```sh
~/.local/bin/nf auth begin --json
```

Expected: exit 0 and

```json
{"verification_uri": "...", "verification_uri_complete": "...",
 "user_code": "...", "expires_in": 900}
```

It returns at once; the approval is finished by step 6.

Tell the person: "Please open <verification_uri_complete> (or open <verification_uri> and
enter the code <user_code>) and approve with your NevoFlux account. It expires in
<expires_in / 60> minutes."

Other outcomes:

- `{"already_authorized": true}`: already signed in. Go to step 6.
- exit 1 and `{"error": "..."}`: the account service could not be reached or the state file
  is unreadable. Wait a minute and retry. If the error says the file is unreadable or from a
  newer version, first upgrade (see "Upgrade") and retry. Only if that does not help, run
  `nf reset --local-only`, and only after telling the person and getting their agreement.
  Tell the person: "A stored file of the NevoFlux connection is unreadable. To start over I
  must delete it locally; the old sign-in may then stay valid on the NevoFlux server until it
  expires. Do you agree?" Then start again from step 4.

## 6. Wait for the connection

Run this every 5 seconds until `state` is `connected`:

```sh
~/.local/bin/nf status --json
```

`nf status` exits 0 whenever it can report. Read the `state` field. Other fields:
`paired`, `auth` (`ok`, `pending` or `none`), `supervision`, and when relevant
`user_code`, `verification_uri`, `verification_uri_complete`, `expires_in` (seconds left),
`last_error`, `poll_error`, `bridge` (`{"pid", "since"}`), `bridge_error`, `head`.

- `awaiting_device_approval`: the person has not approved yet. Keep polling. If more than
  `expires_in` seconds pass, the sign-in expires and `state` becomes `not_authorized`.
  `last_error` (for example `expired_token`) appears only in the one `nf status` call that
  finds the failure; later calls show plain `not_authorized`. On any `not_authorized`, repeat
  step 5 and give the person the new link (mention `last_error` if you saw it).
- `ready`, `connecting`: keep polling.
- `connected`: done.
- Anything else: look the state up in the table below.

Tell the person (when `connected`): "Connected. I can now use your NevoFlux browser."

If the state stays `head_offline` or `connecting` for more than 5 minutes, stop polling and
tell the person. Tell the person: "I cannot reach your NevoFlux browser. Is NevoFlux open, and
is this agent still listed there? If not, please run `/pair-agent` and send me a fresh
block." Other states follow the table.

## 7. Self-check

```sh
~/.local/bin/nf version
~/.local/bin/nf tools
```

Expected: `nevoflux-muse 0.2.0 (protocol 1-1)`, then one line per browser tool, for example `browser_snapshot`
and `browser_navigate`. With `--json`, `nf tools` prints a JSON array of tool objects.

If `nf tools` exits 1, run `nf status --json` and use the table below.

Try one call if you like:

```sh
~/.local/bin/nf call browser_snapshot
```

`nf call <tool> key=value ...` takes values as JSON when they parse as JSON and as strings
otherwise. For arguments that may be secret use
`printf '%s' '{"text": "..."}' | nf call browser_type --args-stdin`. `--timeout <seconds>`
defaults to 120. Exit 1 means the call failed or the tool reported an error; the message is
in the output.

## `nf status` states

| state | meaning | what to do |
| --- | --- | --- |
| `awaiting_device_approval` | A sign-in is waiting for the person to approve it. | Give the person the `verification_uri_complete` and `user_code` from the output again; keep polling. After `expires_in` it lapses (see step 6). |
| `not_paired` | No browser is paired. | Do step 4. |
| `not_authorized` | Paired, but not signed in (or the last sign-in expired or was denied; `last_error` shows it only in the call that found it). | Do step 5. |
| `ready` | Paired and signed in; the bridge has not reported yet. | Poll again in a few seconds. |
| `connecting` | The bridge is dialing the browser. | Poll again. If it lasts more than a few minutes, see `head_offline`. |
| `head_offline` | The browser is not reachable: NevoFlux is closed, or the agent was removed there. | Tell the person: "NevoFlux does not seem to be open, or this agent was removed in it. Please open NevoFlux; if the agent is gone from its list, send me a new `/pair-agent` block." Keep polling; it reconnects by itself when the browser returns. |
| `connected` | Working. | Use `nf tools` and `nf call`. |
| `incompatible` | The browser speaks another agent protocol version. | Upgrade (see "Upgrade"). If it persists, tell the person: "NevoFlux and my connection software are on incompatible versions; I need a newer release." |
| `auth_revoked` | The account sign-in is no longer valid. | Run `nf auth begin --reset --json` and do step 5 again. |
| `account_mismatch` | Signed in with a different NevoFlux account than the browser's. | Run `nf auth begin --reset --json`, and tell the person: "Please approve with the same NevoFlux account that the browser is signed in to." |
| `relay_refused` | The relay refused the channel. | Pair again (step 4). Tell the person: "Please run `/pair-agent` again and send me the new block." |
| `error` | The bridge hit an unexpected error. | Read `last_error` and `~/.local/state/nevoflux-muse/bridge.log`; under `systemd-user` supervision also `journalctl --user -u nevoflux-muse.service`. Run `nf ensure --json`. If it repeats, report it to the person. |
| `not_ready` | The bridge reports it has no pairing or sign-in. | Run `nf status --json` again and follow `paired` and `auth`; do steps 4 and 5 as needed. |
| `bridge_down` | The bridge is not running and could not be started; `bridge_error` says why. | Run `nf ensure --json`, then check `bridge.out` (unused under `systemd-user` supervision: use `journalctl --user -u nevoflux-muse.service` there) and `bridge.log` in `~/.local/state/nevoflux-muse/`, and `install.log`. If the error mentions an unreadable state file, see "Change pairing or account". |

An occasional `connecting` or `head_offline` comes from the hourly VM rebuild. Do not
reinstall: wait and keep polling, the bridge reconnects by itself and the watchdog restarts
it.

`nf ensure --json` prints `{"running": true, "started": true|false}`, or
`{"running": false, "reason": "not_ready"}` (exit 0, not paired or not signed in yet), or
`{"running": false, "reason": "failed", "error": "..."}` (exit 1).

## Logs

| file | what it holds |
| --- | --- |
| `~/.local/share/nevoflux-muse/install.log` | `setup.sh` and the watchdog (`ensure.sh`): what ran and what `nf ensure` returned. Trimmed when it grows large. |
| `~/.local/share/nevoflux-muse/pip.out` | The output of the last environment build. |
| `~/.local/state/nevoflux-muse/bridge.log` | The bridge: tool names, outcomes and timings. Never arguments or results. Rotated at 1 MiB (`bridge.log.1` to `.3`). |
| `~/.local/state/nevoflux-muse/bridge.out` | Standard output and error of a detached bridge, from its last start. |

`~/.local/state/nevoflux-muse` is the state directory unless `NF_HOME` or `XDG_STATE_HOME`
is set. Do not paste the content of other files in it (pairing, account) anywhere.

## Upgrade

Pick the new tag, `vNEW`, then:

```sh
git -C ~/.local/share/nevoflux-muse/src fetch --depth 1 origin tag vNEW
git -C ~/.local/share/nevoflux-muse/src checkout vNEW
~/.local/share/nevoflux-muse/src/setup.sh
```

`setup.sh` is idempotent. It rebuilds the environment, refreshes `ensure.sh`, and restarts a
running bridge so the new code takes over. Pairing and sign-in are kept. Check with
`nf version` and `nf status --json`.

If `git fetch` or `checkout` fails, nothing has changed; fix the network or the tag name and
try again.

## Change pairing or account

- New browser or new pairing code: just run step 4 again with the new block (it replaces
  the old pairing). To forget the pairing first: `nf unpair --json` prints
  `{"unpaired": true|false, "state_dir": "..."}`; the account sign-in stays.
  Tell the person: "Please also remove the old agent in NevoFlux (`/unpair`): it still lists
  it."
- Different NevoFlux account: `nf auth begin --reset --json` revokes the current sign-in
  and starts a new one. Then do step 5's "Tell the person" and step 6. If it exits 1 with
  an error, the old token could not be revoked: retry later.
- Starting over completely, keeping the install: `nf reset --json` stops the bridge, revokes
  the account token on the server and checks it is dead, then deletes the state. It prints
  `{"revoked": ..., "removed": [...], "left": [...], "state_dir": "..."}`. If it exits 1
  (`revoked` is `false`) the account service could not be reached: run it again later. Use
  `nf reset --local-only` only if the person agrees the token may stay valid on the server.
  `nf reset` also deletes the `supervision` file: run `~/.local/bin/nf setup --json` again
  (before pairing again) to restore it. Then start again at step 4.

## Uninstall

```sh
~/.local/bin/nf uninstall --json
```

If `nf` itself is broken, `~/.local/share/nevoflux-muse/src/uninstall.sh` repairs the
environment and then runs `nf uninstall`.

It stops the bridge and removes the systemd unit, revokes the sign-in on the server and
checks it is dead, deletes the state directory's files, deletes the install root
`~/.local/share/nevoflux-muse`, and removes the `~/.local/bin/nf` link. Expected exit 0 and
`{"ok": true, "steps": [...], "next_steps": [...]}`. The `steps` entries are named `service`
(only with systemd), `revoke`, `state`, `install_root` and `link`.

Then:

Tell the person: "The NevoFlux connection is uninstalled on my side. Please remove this agent
in NevoFlux with `/unpair`, since the browser still lists it."

And delete the Muse scheduled task that runs `ensure.sh` (step 3).

If it fails: exit 1 and `{"ok": false, "steps": [...], "next_steps": []}`, with the failed
step carrying an `error`. The usual cause is a revoke failure (the account service was not
reachable): the account sign-in is kept so you can retry. Wait and run `nf uninstall`
again. Do not delete files by hand: that would leave a valid token on the server.

## Secrets

- The pairing block, the account token and the channel key are secrets. They live only in
  the state directory (mode 0700, files 0600). `nf` never prints them and never accepts them
  as arguments.
- Do not read or copy files from the state directory, and do not echo the pairing block into
  your logs or into chat after use.
- The install scripts never touch the state directory. Only `nf` does.
- Do not delete the state directory by hand: use `nf reset` or `nf uninstall`, which revoke
  the token first.
