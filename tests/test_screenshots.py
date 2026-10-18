import base64
import os
import sys

import pytest

from nevoflux_muse import screenshots
from nevoflux_muse.testing.fake_head import JPEG_B64, TASK_ID


def block(data=JPEG_B64, mime="image/jpeg"):
    return {"type": "image", "data": data, "mimeType": mime}


def test_save_writes_the_image_under_the_state_dir(tmp_path):
    d = tmp_path / "nf"
    path = screenshots.save(d, TASK_ID, block())
    assert path == d / "screenshots" / f"{TASK_ID}.jpg"
    assert path.read_bytes() == base64.b64decode(JPEG_B64)
    assert screenshots.find(d, TASK_ID) == path
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temp file left


def test_an_uppercase_id_is_saved_lowercase(tmp_path):
    path = screenshots.save(tmp_path, TASK_ID.upper(), block())
    assert path.name == f"{TASK_ID}.jpg"
    assert screenshots.find(tmp_path, TASK_ID.upper()) == path


def test_png_is_refused(tmp_path):
    with pytest.raises(screenshots.Unsaved) as e:
        screenshots.save(tmp_path, TASK_ID, block(mime="image/png"))
    assert e.value.reason == "unsupported_image"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_save_is_private(tmp_path):
    path = screenshots.save(tmp_path / "nf", TASK_ID, block())
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(("task_id", "image", "reason"), [
    ("../../outside", block(), "bad_task_id"),
    (None, block(), "bad_task_id"),
    (TASK_ID, block(mime="image/gif"), "unsupported_image"),
    (TASK_ID, block(data="not base64!"), "bad_image"),
    (TASK_ID, block(data=""), "bad_image"),
    (TASK_ID, block(data=base64.b64encode(b"garbage, not a jpeg").decode()), "bad_image"),
    (TASK_ID, block(data=base64.b64encode(b"\xff\xd8\xff" + bytes(2 * 1024 * 1024)).decode()),
     "bad_image"),
    (TASK_ID, "not a dict", "bad_image"),
    (TASK_ID, block(mime=["image/jpeg"]), "unsupported_image"),
    (TASK_ID, {"type": "image", "mimeType": "image/jpeg"}, "bad_image"),
])
def test_save_refuses(tmp_path, task_id, image, reason):
    with pytest.raises(screenshots.Unsaved) as e:
        screenshots.save(tmp_path, task_id, image)
    assert e.value.reason == reason
    assert list(tmp_path.iterdir()) == []  # nothing written anywhere


def test_a_failed_write_leaves_no_temp_file(tmp_path, monkeypatch):
    def refuse(src, dst):
        raise PermissionError("denied")

    monkeypatch.setattr(screenshots.os, "replace", refuse)
    with pytest.raises(screenshots.Unsaved) as e:
        screenshots.save(tmp_path, TASK_ID, block())
    assert e.value.reason == "save_failed"
    assert list((tmp_path / "screenshots").iterdir()) == []


def test_only_the_newest_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(screenshots, "KEEP", 2)
    ids = ["00000000-0000-4000-8000-00000000000" + str(n) for n in range(3)]
    for n, task_id in enumerate(ids):
        path = screenshots.save(tmp_path, task_id, block())
        os.utime(path, (1000 + n, 1000 + n))
    assert screenshots.find(tmp_path, ids[0]) is None
    assert screenshots.find(tmp_path, ids[1]) and screenshots.find(tmp_path, ids[2])


def test_find_never_builds_a_path_from_a_bad_id(tmp_path):
    assert screenshots.find(tmp_path, "../x") is None and screenshots.find(tmp_path, 5) is None


def test_a_prune_failure_after_the_write_is_not_a_save_failure(tmp_path, monkeypatch):
    def boom(where):
        raise OSError("prune")

    monkeypatch.setattr(screenshots, "_prune", boom)
    path = screenshots.save(tmp_path, TASK_ID, block())
    assert path.is_file()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_save_refuses_a_symlinked_folder(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    d = tmp_path / "nf"
    d.mkdir()
    (d / "screenshots").symlink_to(elsewhere)
    with pytest.raises(screenshots.Unsaved) as e:
        screenshots.save(d, TASK_ID, block())
    assert e.value.reason == "save_failed"
    assert list(elsewhere.iterdir()) == []
