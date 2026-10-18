"""Where the client keeps what it knows, and how to forget it.

One agent pairs with one head. The state directory holds:

- `pairing.json`: relay, channel and the derived channel key (never the code)
- `account.json`: the NevoFlux account token from the device grant, and the
  account service that issued it (revocation must go back there)
- `auth_pending.json`: a device grant waiting for the person to approve it
- `supervision`: how the bridge is kept running (set by `nf setup`)
- the bridge's socket, lock, log and output (see daemon.py)
- `screenshots/`: the result images of delegated tasks that `nf task wait`
  saved (screenshots.py); the person's data, so private like the rest

All are private: the directory is created 0700 and the files 0600, written
whole or not at all. Each JSON file carries a schema version; a file this
client cannot read is an error, never a guess. Nothing else belongs here, and
`reset` only ever deletes these names (and, in `screenshots/`, only the files
named like ours), so pointing NF_HOME at a directory with other files in it is
harmless.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

APP = "nevoflux-muse"
PAIRING_FILE = "pairing.json"
ACCOUNT_FILE = "account.json"
AUTH_PENDING_FILE = "auth_pending.json"
SUPERVISION_FILE = "supervision"
SUPERVISION_MODES = ("systemd-user", "detached+watchdog")
BRIDGE_FILES = ("bridge.sock", "bridge.lock", "bridge.out", "bridge.log",
                "bridge.log.1", "bridge.log.2", "bridge.log.3")
KNOWN_FILES = (ACCOUNT_FILE, AUTH_PENDING_FILE, PAIRING_FILE, SUPERVISION_FILE,
               *BRIDGE_FILES)
SCREENSHOTS_DIR = "screenshots"
SCREENSHOT_NAME = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(?:jpg|png)")
SCREENSHOT_TEMP = ".shot."
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


def ensure_dir(d: Path) -> None:
    if not d.exists():
        d.mkdir(parents=True)
        os.chmod(d, 0o700)  # mkdir's mode is masked by the umask


def write_secret(d: Path, name: str, obj: dict) -> None:
    ensure_dir(d)
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


def supervision(d: Path) -> str | None:
    try:
        mode = (d / SUPERVISION_FILE).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return mode if mode in SUPERVISION_MODES else None


def write_supervision(d: Path, mode: str) -> None:
    if mode not in SUPERVISION_MODES:
        raise ValueError(f"unknown supervision mode {mode!r}")
    ensure_dir(d)
    path = d / SUPERVISION_FILE
    path.write_text(mode + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def unpair(d: Path) -> bool:
    """Forget the pairing, keep the account. True if there was one."""
    try:
        (d / PAIRING_FILE).unlink()
    except FileNotFoundError:
        return False
    return True


def _reset_screenshots(d: Path) -> bool:
    """Delete the task screenshots we saved; True if the folder went too.

    Only regular files named like ours go: a symlink is skipped (never followed),
    a directory is never touched, and a symlinked folder is left alone. Each entry fails on its own.
    """
    where = d / SCREENSHOTS_DIR
    if where.is_symlink():
        return False
    try:
        entries = list(where.iterdir())
    except OSError:
        return False
    for entry in entries:
        if not (SCREENSHOT_NAME.fullmatch(entry.name) or entry.name.startswith(SCREENSHOT_TEMP)):
            continue
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
            entry.unlink()
        except OSError:
            continue
    try:
        where.rmdir()
    except OSError:
        return False  # it holds something we did not write: leave it
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
    if SCREENSHOTS_DIR not in keep and _reset_screenshots(d):
        removed.append(SCREENSHOTS_DIR)
    try:
        left = sorted(p.name for p in d.iterdir())
    except FileNotFoundError:
        return removed, []
    if not left:
        d.rmdir()
    return removed, left
