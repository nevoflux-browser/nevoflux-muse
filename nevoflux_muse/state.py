"""Where the client keeps what it knows, and how to forget it.

One agent pairs with one head. The state directory holds:

- `pairing.json`: relay, channel and the derived channel key (never the code)
- `account.json`: the NevoFlux account token from the device grant, and the
  account service that issued it (revocation must go back there)
- `auth_pending.json`: a device grant waiting for the person to approve it

All are secrets: the directory is created 0700 and the files 0600, written
whole or not at all. Each carries a schema version; a file this client cannot
read is an error, never a guess. Nothing else belongs here, and `reset` only
ever deletes these names, so pointing NF_HOME at a directory with other files
in it is harmless.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

APP = "nevoflux-muse"
PAIRING_FILE = "pairing.json"
ACCOUNT_FILE = "account.json"
AUTH_PENDING_FILE = "auth_pending.json"
KNOWN_FILES = (ACCOUNT_FILE, AUTH_PENDING_FILE, PAIRING_FILE)
SCHEMA = 1
NUMBER = (int, float)  # a field type: JSON numbers (never bool)


class UnsupportedState(Exception):
    """A state file this client cannot read."""

    def __init__(self, name: str):
        super().__init__(
            f"{name} is unreadable or from a newer nevoflux-muse; upgrade, or run "
            f"`nf reset --local-only` to start over"
        )


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


def write_secret(d: Path, name: str, obj: dict) -> None:
    if not d.exists():
        d.mkdir(parents=True)
        os.chmod(d, 0o700)  # mkdir's mode is masked by the umask
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{name}.")
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({**obj, "v": SCHEMA}, f)
        os.replace(tmp, d / name)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def read_state(d: Path, name: str, fields: Mapping[str, type | tuple[type, ...]] | None = None
               ) -> dict | None:
    """The file's object, None if absent. `fields` maps each required name to its type: a
    missing field or a value of the wrong type is as unreadable as bad JSON."""
    try:
        text = (d / name).read_text(encoding="utf-8")
        obj = json.loads(text)
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeDecodeError):
        raise UnsupportedState(name) from None
    if not isinstance(obj, dict) or obj.get("v") != SCHEMA:
        raise UnsupportedState(name)
    for field, kind in (fields or {}).items():
        value = obj.get(field)
        if isinstance(value, bool) or not isinstance(value, kind):
            raise UnsupportedState(name)
        if isinstance(value, float) and not math.isfinite(value):
            raise UnsupportedState(name)
    return obj


def unpair(d: Path) -> bool:
    """Forget the pairing, keep the account. True if there was one."""
    try:
        (d / PAIRING_FILE).unlink()
    except FileNotFoundError:
        return False
    return True


def reset(d: Path, keep: tuple[str, ...] = ()) -> tuple[list[str], list[str]]:
    """Forget what this client wrote, except `keep`. Returns (removed, left behind)."""
    removed = []
    for name in KNOWN_FILES:
        if name in keep:
            continue
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
