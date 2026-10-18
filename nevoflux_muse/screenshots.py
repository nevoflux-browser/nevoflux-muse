"""Task screenshots, saved where `nf task wait` can name them.

A delegated task that ends `done` may carry one JPEG of what it produced (M3
contract R12); the head sends it once per task and MCP session. `nf task wait`
writes it to `<state dir>/screenshots/<task_id>.jpg` and prints the path, so the
agent can look at it or pass it to the person. It is the person's data: the
folder is 0700 and the files 0600, like the rest of the state directory, and
only the newest KEEP are kept. The task id names the file, so it must be a
UUID; anything else is refused rather than turned into a path.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import logging
import os
import tempfile
from pathlib import Path

from . import state, tasks

log = logging.getLogger(__name__)

KEEP = 50
SUFFIX = {"image/jpeg": ".jpg"}  # contract R12: one JPEG
MAX_BYTES = 2 * 1024 * 1024
JPEG_MAGIC = b"\xff\xd8\xff"


class Unsaved(Exception):
    """The image was not written; `reason` is a short code for --json output."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _folder(d: Path) -> Path:
    return d / state.SCREENSHOTS_DIR


def find(d: Path, task_id: object) -> Path | None:
    """The saved screenshot of this task, if one is on disk."""
    if not tasks.is_task_id(task_id):
        return None
    for suffix in SUFFIX.values():
        path = _folder(d) / f"{task_id.lower()}{suffix}"
        if path.is_file():
            return path
    return None


def save(d: Path, task_id: object, block: dict) -> Path:
    """Write an MCP image block for this task; the path, or Unsaved with the reason."""
    if not tasks.is_task_id(task_id):
        raise Unsaved("bad_task_id")
    if not isinstance(block, dict):
        raise Unsaved("bad_image")
    mime = block.get("mimeType")
    suffix = SUFFIX.get(mime) if isinstance(mime, str) else None
    if suffix is None:
        raise Unsaved("unsupported_image")
    data = block.get("data")
    try:
        raw = base64.b64decode(data, validate=True) if isinstance(data, str) else b""
    except (binascii.Error, ValueError):
        raw = b""
    if not raw.startswith(JPEG_MAGIC) or len(raw) > MAX_BYTES:
        raise Unsaved("bad_image")
    where = _folder(d)
    target = where / f"{task_id.lower()}{suffix}"
    try:
        state.ensure_dir(d)
        if where.is_symlink():
            raise OSError("screenshots folder is a symlink")
        state.ensure_dir(where)
        fd, tmp = tempfile.mkstemp(dir=where, prefix=state.SCREENSHOT_TEMP)
        try:
            try:
                os.chmod(tmp, 0o600)
            except BaseException:
                os.close(fd)
                raise
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            os.replace(tmp, target)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
    except OSError:
        raise Unsaved("save_failed") from None
    # The image is on disk: a failure to tidy older ones is not a failed save.
    try:
        _prune(where)
    except OSError:
        log.warning("could not prune old task screenshots")
    return target


def _prune(where: Path) -> None:
    ours = [p for p in where.iterdir() if state.SCREENSHOT_NAME.fullmatch(p.name) and p.is_file()
            and not p.is_symlink()]
    ours.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for old in ours[KEEP:]:
        old.unlink(missing_ok=True)
