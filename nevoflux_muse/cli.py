"""The `nf` command.

Every subcommand is non-interactive and takes --json, because the one running
it is usually an agent, not a person at a terminal.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import PROTOCOL_MAX, PROTOCOL_MIN, __version__, auth, pairing, state


def _version(args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps({"client": __version__,
                          "protocol": {"min": PROTOCOL_MIN, "max": PROTOCOL_MAX}}))
    else:
        print(f"nevoflux-muse {__version__} (protocol {PROTOCOL_MIN}-{PROTOCOL_MAX})")
    return 0


def _failed(args: argparse.Namespace, e: Exception) -> int:
    if args.json:
        print(json.dumps({"error": str(e)}))
    else:
        print(f"nf: {e}", file=sys.stderr)
    return 1


def _unpair(args: argparse.Namespace) -> int:
    d = state.state_dir()
    try:
        unpaired = state.unpair(d)
    except OSError as e:
        return _failed(args, e)
    if args.json:
        print(json.dumps({"unpaired": unpaired, "state_dir": str(d)}))
    elif unpaired:
        # The protocol has no way to tell the head, so the browser keeps
        # listing this agent; without the key it can no longer connect.
        print("Unpaired. Remove this agent in NevoFlux too.")
    else:
        print("Not paired.")
    return 0


def _reset(args: argparse.Namespace) -> int:
    d = state.state_dir()
    revoked: bool | None = None
    error = None
    if not args.local_only:
        try:
            revoked = auth.revoke_and_verify(d) or None
        except (auth.AccountError, state.UnsupportedState, OSError) as e:
            revoked, error = False, str(e)
    try:
        removed, left = state.reset(d, keep=(state.ACCOUNT_FILE,) if revoked is False else ())
    except OSError as e:
        return _failed(args, e)
    if args.json:
        out = {"revoked": revoked, "removed": removed, "left": left, "state_dir": str(d)}
        if error:
            out["error"] = error
        print(json.dumps(out))
    else:
        if revoked is False:
            print(f"Could not revoke the account token: {error}. Kept {state.ACCOUNT_FILE}: run "
                  f"`nf reset` again when the account service is reachable, or "
                  f"`nf reset --local-only` to delete it anyway (it then stays valid on the "
                  f"server until it expires).", file=sys.stderr)
        print(f"Removed {', '.join(removed)} from {d}." if removed else "Nothing to remove.")
        if left:
            print(f"Left {d} in place: it also holds {', '.join(left)}.")
    return 1 if revoked is False else 0


def _pair(args: argparse.Namespace) -> int:
    d = state.state_dir()
    try:
        block = pairing.pair(sys.stdin.read(), d)
    except (ValueError, OSError) as e:
        return _failed(args, e)
    if args.json:
        print(json.dumps({"paired": True, "relay": block.relay,
                          "channel": block.channel}))
    else:
        print(f"Paired with channel {block.channel} on {block.relay}.")
    return 0


def _auth_begin(args: argparse.Namespace) -> int:
    d = state.state_dir()
    try:
        if state.read_state(d, state.ACCOUNT_FILE) is not None:
            if not args.reset:
                print(json.dumps({"already_authorized": True}) if args.json
                      else "Already authorized. Use --reset to sign in again.")
                return 0
            auth.revoke_and_verify(d)
            (d / state.ACCOUNT_FILE).unlink(missing_ok=True)
        dc = auth.begin(d, auth.AccountClient(auth.account_url()))
    except (auth.AccountError, state.UnsupportedState, OSError) as e:
        return _failed(args, e)
    if args.json:
        print(json.dumps({
            "verification_uri": dc.verification_uri,
            "verification_uri_complete": dc.verification_uri_complete,
            "user_code": dc.user_code,
            "expires_in": dc.expires_in,
        }))
    else:
        print(f"Open {dc.verification_uri_complete} (or {dc.verification_uri} and enter "
              f"{dc.user_code}) and approve with your NevoFlux account. The code expires in "
              f"{dc.expires_in // 60} minutes; `nf status` reports when it is approved.")
    return 0


def _status(args: argparse.Namespace) -> int:
    d = state.state_dir()
    try:
        outcome = auth.poll_if_due(d)
        pending = state.read_state(d, state.AUTH_PENDING_FILE, auth.PENDING_FIELDS)
        paired = state.read_state(d, state.PAIRING_FILE, pairing.PAIRING_FIELDS) is not None
        authorized = state.read_state(d, state.ACCOUNT_FILE, auth.ACCOUNT_FIELDS) is not None
    except (state.UnsupportedState, OSError) as e:
        return _failed(args, e)
    out: dict = {"paired": paired, "auth": "ok" if authorized else "pending" if pending else "none"}
    if pending is not None:
        out["state"] = "awaiting_device_approval"
        out["user_code"] = pending["user_code"]
        out["verification_uri"] = pending["verification_uri"]
        out["verification_uri_complete"] = pending["verification_uri_complete"]
        out["expires_in"] = max(0, int(pending["expires_at"] - auth.now()))
        if outcome is not None and outcome.poll_error:
            out["poll_error"] = outcome.poll_error
    elif not paired:
        out["state"] = "not_paired"
    elif not authorized:
        out["state"] = "not_authorized"
    else:
        out["state"] = "ready"
    if outcome is not None and outcome.error:
        out["last_error"] = outcome.error
    if args.json:
        print(json.dumps({"state": out.pop("state"), **out}))
    else:
        text = _STATUS_TEXT[out["state"]].format(**out)
        if "last_error" in out:
            text += f" (last attempt: {out['last_error']})"
        if "poll_error" in out:
            text += f" ({out['poll_error']})"
        print(text)
    return 0


_STATUS_TEXT = {
    "awaiting_device_approval": "Waiting for approval: open {verification_uri_complete} "
                                "(code {user_code}, {expires_in}s left).",
    "not_paired": "Not paired: pipe the /pair-agent block into `nf pair --from-stdin`.",
    "not_authorized": "Not signed in: run `nf auth begin`.",
    "ready": "Paired and signed in.",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nf")
    sub = parser.add_subparsers(dest="command")

    def command(parent, name, run, summary):
        cmd = parent.add_parser(name, help=summary)
        cmd.add_argument("--json", action="store_true")
        cmd.set_defaults(run=run)
        return cmd

    command(sub, "version", _version, "print client and protocol versions")
    pair_cmd = command(sub, "pair", _pair, "pair with a browser: pipe in the block from /pair-agent")
    pair_cmd.add_argument("--from-stdin", action="store_true", required=True,
                          help="read the pairing block from stdin (never from an argument)")
    auth_cmd = sub.add_parser("auth", help="sign in to the NevoFlux account")
    begin = command(auth_cmd.add_subparsers(dest="auth_command"), "begin", _auth_begin,
                    "start a device sign-in and return at once; `nf status` finishes it")
    begin.add_argument("--reset", action="store_true",
                       help="revoke the current sign-in first (to switch accounts)")
    command(sub, "status", _status, "where pairing and sign-in stand")
    command(sub, "unpair", _unpair, "forget the paired browser, keep the account login")
    reset = command(sub, "reset", _reset,
                    "revoke the sign-in and forget everything (run before uninstalling)")
    reset.add_argument("--local-only", action="store_true",
                       help="delete local state without revoking on the server")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "run"):
        parser.print_usage(sys.stderr)
        return 2
    try:
        return args.run(args)
    except Exception as e:  # noqa: BLE001 - last resort; str(e) could carry a secret
        name = type(e).__name__
        if getattr(args, "json", False):
            print(json.dumps({"error": f"unexpected {name}"}))
        else:
            print(f"nf: unexpected {name}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
