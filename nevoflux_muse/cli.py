"""The `nf` command.

Every subcommand is non-interactive and takes --json, because the one running
it is usually an agent, not a person at a terminal.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import PROTOCOL_MAX, PROTOCOL_MIN, __version__, state


def _version(args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps({"client": __version__,
                          "protocol": {"min": PROTOCOL_MIN, "max": PROTOCOL_MAX}}))
    else:
        print(f"nevoflux-muse {__version__} (protocol {PROTOCOL_MIN}-{PROTOCOL_MAX})")
    return 0


def _failed(args: argparse.Namespace, e: OSError) -> int:
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
    try:
        removed, left = state.reset(d)
    except OSError as e:
        return _failed(args, e)
    if args.json:
        print(json.dumps({"removed": removed, "left": left, "state_dir": str(d)}))
    else:
        print(f"Removed {', '.join(removed)} from {d}." if removed else "Nothing to remove.")
        if left:
            print(f"Left {d} in place: it also holds {', '.join(left)}.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nf")
    sub = parser.add_subparsers(dest="command")
    for name, run, summary in (
        ("version", _version, "print client and protocol versions"),
        ("unpair", _unpair, "forget the paired browser, keep the account login"),
        ("reset", _reset, "forget everything this client stored (run before uninstalling)"),
    ):
        cmd = sub.add_parser(name, help=summary)
        cmd.add_argument("--json", action="store_true")
        cmd.set_defaults(run=run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "run"):
        parser.print_usage(sys.stderr)
        return 2
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
