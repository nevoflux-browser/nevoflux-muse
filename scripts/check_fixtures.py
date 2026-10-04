"""Check that fixtures/muse is byte-identical to the daemon commit it pins.

    python scripts/check_fixtures.py                    # against GitHub
    python scripts/check_fixtures.py --local ../agent   # against a local checkout

PROTOCOL_VERSION is compared first: when it differs the protocol moved, and a
list of byte differences would only bury that.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures" / "muse"


class NotFound(Exception):
    pass


def _get(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound(url) from e
        raise


def fetch_remote(repo: str, commit: str, path: str, names: list[str]) -> dict[str, bytes]:
    out = {}
    for name in names:
        url = f"https://raw.githubusercontent.com/{repo}/{commit}/{path}/{name}"
        try:
            out[name] = _get(url)
        except NotFound:
            raise SystemExit(
                f"{url} not found: commit {commit} is not on GitHub yet "
                f"(has it been pushed?), or the fixture was removed there."
            )
    return out


def fetch_local(agent: pathlib.Path, commit: str, path: str, names: list[str]) -> dict[str, bytes]:
    return {
        name: subprocess.run(
            ["git", "-C", str(agent), "show", f"{commit}:{path}/{name}"],
            check=True, capture_output=True,
        ).stdout
        for name in names
    }


def compare(local: dict[str, bytes], upstream: dict[str, bytes]) -> list[str]:
    if local.get("PROTOCOL_VERSION") != upstream.get("PROTOCOL_VERSION"):
        return [
            "protocol upgraded upstream "
            f"({local.get('PROTOCOL_VERSION', b'?').strip().decode()} -> "
            f"{upstream.get('PROTOCOL_VERSION', b'?').strip().decode()}): "
            "sync fixtures/muse and the client before anything else"
        ]
    problems = []
    for name in sorted(set(local) | set(upstream)):
        if name not in upstream:
            problems.append(f"{name} is not upstream")
        elif name not in local:
            problems.append(f"{name} is missing locally")
        elif local[name] != upstream[name]:
            problems.append(f"{name} differs from upstream")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", type=pathlib.Path, help="agent repo checkout")
    args = parser.parse_args(argv)

    src = json.loads((ROOT / "fixtures" / "SOURCE.json").read_text())
    names = sorted(p.name for p in FIX.iterdir())
    local = {n: (FIX / n).read_bytes() for n in names}
    if args.local:
        upstream = fetch_local(args.local, src["commit"], src["path"], names)
    else:
        upstream = fetch_remote(src["repo"], src["commit"], src["path"], names)

    problems = compare(local, upstream)
    for p in problems:
        print(p, file=sys.stderr)
    if not problems:
        print(f"fixtures match {src['repo']}@{src['commit'][:12]}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
