"""Presentation metadata and isolated mounted reads through catalog reconciliation."""

import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image, ImageFile
from test_catalog import scan, snapshot, state

from postcardscene import catalog_reconciliation as r
from postcardscene import image_metadata as metadata
from postcardscene import mounted_source as mounted
from postcardscene.filesystem_source import (
    EnumerationFailed,
    ScanCancelled,
    SourceUnavailable,
)

_PRODUCTION_WORKER = mounted._mounted_worker


def _healthy_worker(connection, configuration, policy, operation):
    info = f"1 0 0:1 / {policy.allowed_roots[0]} rw - nfs server:/export rw\n"

    def mount_text(path):
        return "mnt_id:\t1\n" if path.parent == Path("/proc/self/fdinfo") else info

    with patch.object(Path, "read_text", mount_text):
        _PRODUCTION_WORKER(connection, configuration, policy, operation)


def _metadata_stall(connection, configuration, policy, operation):
    if not isinstance(operation, tuple):
        return _healthy_worker(connection, configuration, policy, operation)
    connection.send(("metadata", metadata.ImageMetadata(operation[0], "error")))
    multiprocessing.get_context("spawn").Event().wait()


def _metadata_crash(connection, configuration, policy, operation):
    if not isinstance(operation, tuple):
        return _healthy_worker(connection, configuration, policy, operation)
    os._exit(7)


def _metadata_mount_loss(connection, configuration, policy, operation):
    if not isinstance(operation, tuple):
        return _healthy_worker(connection, configuration, policy, operation)
    info = f"1 0 0:1 / {policy.allowed_roots[0]} rw - cifs server:/export rw\n"
    with patch.object(
        Path, "read_text", side_effect=[info, info, "mnt_id:\t1\n", info, ""]
    ):
        _PRODUCTION_WORKER(connection, configuration, policy, operation)


@pytest.mark.parametrize("rotation", [1, 5, 6, 7, 8])
def test_exif_presentation_without_decoding(catalog, monkeypatch, rotation):
    _, _, root, _ = catalog
    exif = Image.Exif()
    exif[274] = rotation
    Image.new("RGB", (30, 10)).save(root / "image.jpg", exif=exif)
    Image.new("RGB", (8, 8)).save(root / "square.png")

    def no_pixels(*args, **kwargs):
        pytest.fail("Catalog metadata decoded image pixels")

    monkeypatch.setattr(ImageFile.ImageFile, "load", no_pixels)
    scan(catalog)
    expected = (30, 10, "landscape") if rotation == 1 else (10, 30, "portrait")
    assert snapshot(catalog)["image.jpg"][2:5] == expected
    assert snapshot(catalog)["square.png"][2:5] == (8, 8, "square")


@pytest.mark.parametrize("problem", ["corrupt", "bomb_warning", "bomb_error"])
def test_bad_image_is_present_and_does_not_block_removal(catalog, monkeypatch, problem):
    _, _, root, _ = catalog
    (root / "gone.mp4").touch()
    scan(catalog)
    (root / "gone.mp4").unlink()
    if problem == "corrupt":
        (root / "bad.jpg").write_bytes(b"not an image")
    else:
        Image.new("RGB", (10, 10)).save(root / "bad.jpg")
        monkeypatch.setattr(
            Image, "MAX_IMAGE_PIXELS", 60 if problem == "bomb_warning" else 40
        )
    scan(catalog)
    assert set(snapshot(catalog)) == {"bad.jpg"}
    assert snapshot(catalog)["bad.jpg"][1:5] == ("error", None, None, None)
    assert state(catalog)[2] == "ready"
    # An unchanged error remains retryable; a subsequent run does inspect it.
    calls = []
    inspect = r.inspect_image

    def retry(*args):
        calls.append(args[-1])
        return inspect(*args)

    monkeypatch.setattr(r, "inspect_image", retry)
    scan(catalog)
    assert len(calls) == 1


def test_freshness_race_leaves_pending(catalog, monkeypatch):
    _, _, root, _ = catalog
    path = root / "image.jpg"
    Image.new("RGB", (20, 10)).save(path)
    open_image = Image.open

    def changed_during_inspection(*args, **kwargs):
        image = open_image(*args, **kwargs)
        Image.new("RGB", (10, 40)).save(path)
        return image

    with monkeypatch.context() as m:
        m.setattr(Image, "open", changed_during_inspection)
        scan(catalog)
    assert snapshot(catalog)["image.jpg"][1:5] == ("pending", None, None, None)
    scan(catalog)
    assert snapshot(catalog)["image.jpg"][1:5] == ("ready", 10, 40, "portrait")


def test_metadata_does_not_follow_replacement_at_decoder_open(catalog, monkeypatch):
    _, _, root, _ = catalog
    path = root / "image.jpg"
    Image.new("RGB", (20, 10)).save(path)
    outside = root.parent / "private.jpg"
    Image.new("RGB", (90, 80)).save(outside)
    open_image = Image.open
    inspected = []

    def replace_before_decode(stream, **kwargs):
        path.unlink()
        path.symlink_to(outside)
        image = open_image(stream, **kwargs)
        inspected.append(image.size)
        return image

    monkeypatch.setattr(Image, "open", replace_before_decode)
    scan(catalog)
    assert inspected == [(20, 10)]


def test_metadata_rejects_local_file_for_mounted_source(catalog, monkeypatch):
    from postcardscene.filesystem_source import MediaEntry

    _, _, root, policy = catalog
    path = root / "image.jpg"
    Image.new("RGB", (20, 10)).save(path)
    info = path.stat()
    entry = MediaEntry("image.jpg", "image", info.st_size, info.st_mtime_ns)

    def no_decode(*args, **kwargs):
        pytest.fail("Local mountpoint fallback reached the decoder")

    monkeypatch.setattr(Image, "open", no_decode)
    result = metadata.inspect_image(
        "mounted_directory", {"path": str(root), "recursive": True}, policy, entry
    )
    assert result.status == "error"
    assert result.width is None


def _mounted(catalog):
    from postcardscene.domain import Source

    db, source_id, root, policy = catalog
    with db.transaction() as session:
        session.get(Source, source_id).kind = "mounted_directory"
    return db, source_id, root, policy


def test_mounted_catalog_metadata_never_opens_in_parent(catalog, monkeypatch):
    _, _, root, _ = _mounted(catalog)
    for i in range(5):
        Image.new("RGB", (10, 20)).save(root / f"{i}.jpg")
    monkeypatch.setattr(mounted, "_mounted_worker", _healthy_worker)
    batches = []
    inspect = r.inspect_mounted_images

    def checked_batch(*args, **kwargs):
        batches.append(len(args[2]))
        yield from inspect(*args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("Mounted media opened in parent")

    monkeypatch.setattr(r, "inspect_mounted_images", checked_batch)
    monkeypatch.setattr(Image, "open", forbidden)
    monkeypatch.setattr(metadata, "open_image_item", forbidden)
    scan(catalog, metadata_batch_size=2)
    assert batches == [2, 2, 1]
    assert all(
        row[1:5] == ("ready", 10, 20, "portrait") for row in snapshot(catalog).values()
    )


@pytest.mark.parametrize("failure", ["timeout", "cancel", "crash", "mount_loss"])
def test_mounted_metadata_failure_retains_presence(catalog, monkeypatch, failure):
    _, _, root, _ = _mounted(catalog)
    monkeypatch.setattr(mounted, "_mounted_worker", _healthy_worker)
    (root / "gone.mp4").touch()
    scan(catalog)
    (root / "gone.mp4").unlink()
    for i in range(2):
        Image.new("RGB", (10, 20)).save(root / f"{i}.jpg")
    worker = {"crash": _metadata_crash, "mount_loss": _metadata_mount_loss}.get(
        failure, _metadata_stall
    )
    monkeypatch.setattr(mounted, "_mounted_worker", worker)
    save = r._save_metadata
    cancelled = False
    cleaned = []
    cleanup = mounted._cleanup_worker

    def checked_cleanup(process):
        cleanup(process)
        cleaned.append(process._closed)

    def saved(*args):
        nonlocal cancelled
        save(*args)
        if failure == "cancel":
            cancelled = True
        elif failure == "timeout":
            ticks = iter([0.0, 11.0])
            monkeypatch.setattr(
                mounted, "time", SimpleNamespace(monotonic=lambda: next(ticks))
            )

    monkeypatch.setattr(mounted, "_cleanup_worker", checked_cleanup)
    monkeypatch.setattr(r, "_save_metadata", saved)
    error = {"crash": EnumerationFailed, "cancel": ScanCancelled}.get(
        failure, SourceUnavailable
    )
    with pytest.raises(error):
        scan(catalog, cancelled=lambda: cancelled)
    assert set(snapshot(catalog)) == {"0.jpg", "1.jpg"}
    assert snapshot(catalog)["1.jpg"][1] == "pending"
    assert state(catalog)[:3] == (2, 2, "ready")
    assert cleaned == [True, True]


def test_mounted_partial_presence_retains_unseen(catalog, monkeypatch):
    from test_mounted_source import _crashed_worker

    _, _, root, _ = _mounted(catalog)
    (root / "old.mp4").touch()
    monkeypatch.setattr(mounted, "_mounted_worker", _healthy_worker)
    scan(catalog)
    monkeypatch.setattr(mounted, "_mounted_worker", _crashed_worker)
    with pytest.raises(EnumerationFailed):
        scan(catalog, batch_size=1)
    assert set(snapshot(catalog)) == {"old.mp4", "observed.jpg"}
    assert state(catalog)[:3] == (2, 2, "error")
