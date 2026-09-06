"""Mounted adapter contracts using real spawn and controlled child storage."""

import multiprocessing
import os
import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from postcardscene import mounted_source as mounted
from postcardscene.filesystem_source import (
    EnumerationFailed,
    InvalidSource,
    MediaEntry,
    PathPolicy,
    ScanCancelled,
    SourceStatus,
    SourceUnavailable,
    enumerate_local_directory,
)

# Keep the production target available when the parent's target is substituted.
_PRODUCTION_WORKER = mounted._mounted_worker
_ENTRY = MediaEntry("observed.jpg", "image", 1, 2)


def _mount_line(path, kind="nfs", number=1):
    escaped = str(path).replace("\\", r"\134").replace(" ", r"\040")
    escaped = escaped.replace("\t", r"\011").replace("\n", r"\012")
    return f"{number} 0 0:1 / {escaped} rw - {kind} server:/export rw\n"


def _healthy_worker(connection, configuration, policy, enumerate_entries):
    with patch.object(
        Path, "read_text", return_value=_mount_line(policy.allowed_roots[0])
    ):
        _PRODUCTION_WORKER(connection, configuration, policy, enumerate_entries)


def _lost_mount_worker(connection, configuration, policy, enumerate_entries):
    with patch.object(
        Path, "read_text", side_effect=[_mount_line(policy.allowed_roots[0]), ""]
    ):
        _PRODUCTION_WORKER(connection, configuration, policy, enumerate_entries)


def _partial_worker(connection, configuration, policy, enumerate_entries):
    original = os.stat

    def failed_child(path, *args, **kwargs):
        if path == "b.jpg":
            raise OSError("private share details")
        return original(path, *args, **kwargs)

    with (
        patch.object(
            Path, "read_text", return_value=_mount_line(policy.allowed_roots[0])
        ),
        patch.object(os, "stat", side_effect=failed_child),
    ):
        _PRODUCTION_WORKER(connection, configuration, policy, enumerate_entries)


def _stalled_worker(connection, configuration, policy, enumerate_entries):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    connection.send(("entry", _ENTRY))
    multiprocessing.get_context("spawn").Event().wait()


def _crashed_worker(connection, configuration, policy, enumerate_entries):
    connection.send(("entry", _ENTRY))
    os._exit(7)


def _protocol_worker(connection, configuration, policy, enumerate_entries):
    connection.send(("entry", _ENTRY))
    connection.send(("unknown", None))
    connection.close()


@pytest.fixture
def source(tmp_path):
    return {"path": str(tmp_path), "recursive": True}, PathPolicy([tmp_path])


@pytest.fixture
def workers(monkeypatch):
    """Assert real children are reaped for all exits, including forced kill."""
    processes = []
    cleanup = mounted._cleanup_worker

    def checked_cleanup(process):
        processes.append(process)
        cleanup(process)
        assert process._closed

    monkeypatch.setattr(mounted, "_cleanup_worker", checked_cleanup)
    yield processes
    assert processes
    assert all(process._closed for process in processes)


@pytest.mark.parametrize("kind", ["nfs", "nfs4", "cifs"])
def test_nested_network_mount(kind):
    assert mounted._network_mount_covers(
        Path("/mnt/photos/trips/2026"), _mount_line("/mnt/photos", kind)
    )


def test_deepest_mount_and_component_containment():
    info = _mount_line("/", "ext4") + _mount_line("/mnt/photos")
    assert not mounted._network_mount_covers(Path("/mnt/photos-old"), info)
    assert mounted._network_mount_covers(Path("/mnt/photos/child"), info)
    info += _mount_line("/mnt/photos/child", "ext4", 2)
    assert not mounted._network_mount_covers(Path("/mnt/photos/child/trip"), info)


def test_mountpoint_escapes():
    root = Path("/mnt/space tab\tnewline\nback\\slash")
    assert mounted._network_mount_covers(root / "nested", _mount_line(root))


@pytest.mark.parametrize("info", ["", _mount_line("/", "ext4")])
def test_no_network_coverage(info):
    assert not mounted._network_mount_covers(Path("/mnt/photos"), info)


def test_malformed_mountinfo_is_unavailable():
    with pytest.raises(SourceUnavailable):
        mounted._network_mount_covers(Path("/mnt/photos"), "broken")


@pytest.mark.parametrize("recursive", [False, True])
def test_spawned_wrapper_matches_local(
    source, tmp_path, monkeypatch, workers, recursive
):
    config, policy = source
    config["recursive"] = recursive
    for name in ["z.JPG", "a/deep/image.heic", "a/movie.MOV", "b.png", "ignored.txt"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"candidate")
    (tmp_path / "linked.jpg").symlink_to(tmp_path / "z.JPG")
    (tmp_path / "linked-directory").symlink_to(tmp_path / "a")
    os.mkfifo(tmp_path / "special.jpg")
    expected = list(enumerate_local_directory(config, policy))
    monkeypatch.setattr(mounted, "_mounted_worker", _healthy_worker)
    assert list(mounted.enumerate_mounted_directory(config, policy)) == expected


@pytest.mark.parametrize("invalid", ["outside", "recursive", "escape"])
def test_invalid_authority(source, tmp_path, invalid, workers):
    config, policy = source
    if invalid == "outside":
        config["path"] = str(tmp_path.parent)
    elif invalid == "recursive":
        config["recursive"] = 1
    else:
        (tmp_path / "escape").symlink_to(tmp_path.parent)
        config["path"] += "/escape"
    assert mounted.mounted_source_status(config, policy) == SourceStatus.INVALID
    with pytest.raises(InvalidSource):
        list(mounted.enumerate_mounted_directory(config, policy))


def test_underlying_local_directory_is_unavailable(source, workers):
    assert mounted.mounted_source_status(*source) == SourceStatus.UNAVAILABLE
    with pytest.raises(SourceUnavailable):
        list(mounted.enumerate_mounted_directory(*source))


def test_fresh_probes_recover(source, monkeypatch, workers):
    for target, expected in [
        (_PRODUCTION_WORKER, SourceStatus.UNAVAILABLE),
        (_healthy_worker, SourceStatus.AVAILABLE),
        (_PRODUCTION_WORKER, SourceStatus.UNAVAILABLE),
        (_healthy_worker, SourceStatus.AVAILABLE),
    ]:
        monkeypatch.setattr(mounted, "_mounted_worker", target)
        assert mounted.mounted_source_status(*source) == expected
    assert len(workers) == 4


@pytest.mark.parametrize(
    ("target", "error"),
    [
        (_lost_mount_worker, SourceUnavailable),
        (_partial_worker, EnumerationFailed),
        (_crashed_worker, EnumerationFailed),
        (_protocol_worker, EnumerationFailed),
    ],
)
def test_partial_observations_never_exhaust(
    source, tmp_path, monkeypatch, workers, target, error
):
    (tmp_path / "a.jpg").touch()
    (tmp_path / "b.jpg").touch()
    monkeypatch.setattr(mounted, "_mounted_worker", target)
    entries = mounted.enumerate_mounted_directory(*source)
    assert isinstance(next(entries), MediaEntry)
    with pytest.raises(error) as failure:
        list(entries)
    assert "private" not in str(failure.value)


@pytest.mark.parametrize("stop", ["timeout", "cancel", "close"])
def test_hung_child_cleanup(source, monkeypatch, workers, stop):
    monkeypatch.setattr(mounted, "_mounted_worker", _stalled_worker)
    cancelled = False
    # Allow normal spawn startup; shorten only the subsequent idle wait.
    entries = mounted.enumerate_mounted_directory(*source, cancelled=lambda: cancelled)
    assert next(entries) == _ENTRY
    if stop == "close":
        entries.close()
    elif stop == "cancel":
        cancelled = True
        with pytest.raises(ScanCancelled):
            next(entries)
    else:
        ticks = iter([0.0, 11.0])
        monkeypatch.setattr(
            mounted, "time", SimpleNamespace(monotonic=lambda: next(ticks))
        )
        with pytest.raises(SourceUnavailable):
            next(entries)
    assert len(workers) == 1


@pytest.mark.parametrize(
    "timeout", [0, -1, float("nan"), float("inf"), True, "1", None]
)
def test_timeout_policy_is_finite_positive(source, timeout):
    with pytest.raises(ValueError):
        mounted.mounted_source_status(*source, timeout_seconds=timeout)
    with pytest.raises(ValueError):
        list(mounted.enumerate_mounted_directory(*source, timeout_seconds=timeout))


def test_cancel_before_spawn(source):
    with pytest.raises(ScanCancelled):
        next(mounted.enumerate_mounted_directory(*source, cancelled=lambda: True))
