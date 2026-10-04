"""The `nf` command.

Every subcommand is non-interactive and takes --json, because the one running
it is usually an agent, not a person at a terminal.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import PROTOCOL_MAX, PROTOCOL_MIN, __version__


def _version(args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps({"client": __version__,
                          "protocol": {"min": PROTOCOL_MIN, "max": PROTOCOL_MAX}}))
    else:
        print(f"nevoflux-muse {__version__} (protocol {PROTOCOL_MIN}-{PROTOCOL_MAX})")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nf")
    sub = parser.add_subparsers(dest="command")
    version = sub.add_parser("version", help="print client and protocol versions")
    version.add_argument("--json", action="store_true")
    version.set_defaults(run=_version)
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
