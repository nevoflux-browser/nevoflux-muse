"""Where the client keeps what it knows, and how to forget it.

One agent pairs with one head. The state directory holds two files:

- `pairing.json`: relay, channel and the derived channel key (never the code)
- `account.json`: the NevoFlux account tokens from the device grant

Both are secrets: the directory is created 0700 and the files 0600. Nothing
else belongs here, and `reset` only ever deletes these two names, so pointing
NF_HOME at a directory with other files in it is harmless.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP = "nevoflux-muse"
PAIRING_FILE = "pairing.json"
ACCOUNT_FILE = "account.json"
KNOWN_FILES = (ACCOUNT_FILE, PAIRING_FILE)


def state_dir() -> Path:
    """NF_HOME if set, else the platform's per-user state directory."""
    if home := os.environ.get("NF_HOME"):
        return Path(home)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / APP
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    base = os.environ.get("XDG_STATE_HOME")
    # The XDG spec says a relative path is invalid and must be ignored.
    if base and Path(base).is_absolute():
        return Path(base) / APP
    return Path.home() / ".local" / "state" / APP


def unpair(d: Path) -> bool:
    """Forget the pairing, keep the account. True if there was one."""
    try:
        (d / PAIRING_FILE).unlink()
    except FileNotFoundError:
        return False
    return True


def reset(d: Path) -> tuple[list[str], list[str]]:
    """Forget everything this client wrote. Returns (removed, left behind)."""
    removed = []
    for name in KNOWN_FILES:
        try:
            (d / name).unlink()
        except FileNotFoundError:
            continue
        removed.append(name)
    try:
        left = sorted(p.name for p in d.iterdir())
    except FileNotFoundError:
        return removed, []
    if not left:
        d.rmdir()
    return removed, left
