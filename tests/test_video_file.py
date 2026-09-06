"""File capability behavior, using real spawn with controlled mount evidence."""

import fcntl
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from postcardscene import video_file as video
from postcardscene import video_file_worker as worker
from postcardscene.domain import Source
from postcardscene.filesystem_source import PathPolicy, SourceUnavailable
from postcardscene.video_selection import SelectedVideo

_PRODUCTION_WORKER = worker._video_worker


@pytest.fixture
def item(tmp_path):
    path = tmp_path / "movie.mp4"
    path.write_bytes(b"original")
    info = path.stat()
    return (
        SelectedVideo(1, path.name, info.st_size, info.st_mtime_ns),
        video.VideoSource(1, "local_directory", str(tmp_path), True),
        PathPolicy([tmp_path]),
    )


def test_local_capability_and_lifetime(item, tmp_path):
    with video.open_video_item(*item) as fd:
        assert fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        assert not os.get_inheritable(fd)
        (tmp_path / "movie.mp4").rename(tmp_path / "old")
        (tmp_path / "movie.mp4").write_bytes(b"replaced")
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os,sys; os.write(1, os.read(int(sys.argv[1]), 99))",
                str(fd),
            ],
            pass_fds=(fd,),
            capture_output=True,
            check=True,
            timeout=5,
        )
        assert child.stdout == b"original"
    with pytest.raises(OSError):
        os.fstat(fd)


@pytest.mark.parametrize(
    "case",
    [
        "source",
        "kind",
        "recursive",
        "outside",
        "size",
        "mtime",
        "path",
        "symlink",
        "directory_symlink",
        "fifo",
        "directory",
    ],
)
def test_invalid_authority(item, tmp_path, case):
    selected, source, policy = item
    path = tmp_path / selected.relative_path
    if case == "source":
        source = replace(source, source_id=2)
    elif case == "kind":
        source = replace(source, kind="provider")
    elif case == "recursive":
        source = replace(source, recursive=1)
    elif case == "outside":
        policy = PathPolicy([tmp_path / "other"])
    elif case == "size":
        selected = replace(selected, size_bytes=99)
    elif case == "mtime":
        selected = replace(selected, mtime_ns=selected.mtime_ns + 1)
    elif case == "path":
        object.__setattr__(selected, "relative_path", "../movie.mp4")
    elif case == "directory_symlink":
        (tmp_path / "link").symlink_to(tmp_path, target_is_directory=True)
        selected = replace(selected, relative_path="link/movie.mp4")
    else:
        path.unlink()
        if case == "symlink":
            path.symlink_to(tmp_path / "secret")
        elif case == "fifo":
            os.mkfifo(path)
        else:
            path.mkdir()
    before = set(os.listdir("/proc/self/fd"))
    with pytest.raises(video.VideoFileError) as error:
        with video.open_video_item(selected, source, policy):
            pytest.fail("unsafe capability")
    assert error.value.reason == video.VideoFileFailure.INVALID
    assert str(tmp_path) not in str(error.value)
    assert set(os.listdir("/proc/self/fd")) == before


def test_capture_current_source(item, catalog):
    db, source_id, _, _ = catalog
    selected = replace(item[0], source_id=source_id)
    with db.transaction() as session:
        captured = video.capture_video_source(session, selected)
        assert captured.source_id == source_id
        source = session.get(Source, source_id)
        source.enabled = False
        with pytest.raises(video.VideoFileError):
            video.capture_video_source(session, selected)
        source.enabled = True
        source.configuration = {"path": "/new", "recursive": False}
        assert video.capture_video_source(session, selected).path == "/new"
        source.kind = "web"
        with pytest.raises(video.VideoFileError):
            video.capture_video_source(session, selected)


def _healthy_worker(channel, selected, source, policy):
    original = Path.read_text

    def mount_evidence(path, *args, **kwargs):
        if str(path) == "/proc/self/mountinfo":
            # Keep real mount IDs and coverage but substitute filesystem type.
            return "\n".join(
                line.split(" - ")[0] + " - nfs server:/export rw"
                for line in original(path).splitlines()
            )
        return original(path, *args, **kwargs)

    with patch.object(Path, "read_text", mount_evidence):
        _PRODUCTION_WORKER(channel, selected, source, policy)


def _lost_mount_worker(channel, selected, source, policy):
    with patch.object(worker, "_check_open_mount", side_effect=SourceUnavailable):
        with patch.object(worker, "_mounted_directory"):
            _PRODUCTION_WORKER(channel, selected, source, policy)


def _fallthrough_worker(channel, selected, source, policy):
    # Pre-open mount check passed, but the actual opened file is local.
    with patch.object(worker, "_mounted_directory"):
        _PRODUCTION_WORKER(channel, selected, source, policy)


def _stalled_worker(channel, *args):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    channel.send(b"progress")
    time.sleep(60)


def _crashed_worker(channel, *args):
    os._exit(7)


@pytest.fixture
def mounted_item(item, monkeypatch):
    cleaned = []
    cleanup = worker._cleanup_worker

    def checked(process):
        cleanup(process)
        cleaned.append(process._closed)

    monkeypatch.setattr(worker, "_cleanup_worker", checked)
    yield item[0], replace(item[1], kind="mounted_directory"), item[2]
    assert cleaned and all(cleaned)


def test_mounted_fd_transfer_no_parent_storage(mounted_item, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "_video_worker", _healthy_worker)

    def forbidden(*args, **kwargs):
        pytest.fail("storage operation on owner")

    with monkeypatch.context() as parent:
        parent.setattr(Path, "resolve", forbidden)
        parent.setattr(Path, "stat", forbidden)
        parent.setattr(os, "fstat", forbidden)
        with video.open_video_item(*mounted_item) as fd:
            assert not os.get_inheritable(fd)
            (tmp_path / "movie.mp4").rename(tmp_path / "old")
            (tmp_path / "movie.mp4").write_bytes(b"replaced")
            assert os.read(fd, 99) == b"original"
    with pytest.raises(OSError):
        os.fstat(fd)


@pytest.mark.parametrize(
    "target,reason",
    [
        (_PRODUCTION_WORKER, "unavailable"),
        (_lost_mount_worker, "unavailable"),
        (_fallthrough_worker, "unavailable"),
        (_crashed_worker, "helper"),
        (_stalled_worker, "timeout"),
    ],
)
def test_mounted_failures(mounted_item, monkeypatch, target, reason):
    monkeypatch.setattr(worker, "_video_worker", target)
    started = time.monotonic()
    with pytest.raises(video.VideoFileError) as error:
        with video.open_video_item(*mounted_item, timeout_seconds=1):
            pytest.fail("unexpected pin")
    assert error.value.reason == reason
    assert time.monotonic() - started < 4


def test_cancel_received_fd_closes_it(mounted_item, monkeypatch):
    monkeypatch.setattr(worker, "_video_worker", _healthy_worker)
    received = []
    receive = worker._receive_fd

    def capture(*args):
        fd = receive(*args)
        received.append(fd)
        return fd

    monkeypatch.setattr(worker, "_receive_fd", capture)
    with pytest.raises(video.VideoFileError) as error:
        with video.open_video_item(*mounted_item, cancelled=lambda: bool(received)):
            pytest.fail("cancelled pin")
    assert error.value.reason == "cancelled"
    with pytest.raises(OSError):
        os.fstat(received[0])


def test_cancellation_while_waiting(mounted_item, monkeypatch):
    monkeypatch.setattr(worker, "_video_worker", _stalled_worker)
    started = time.monotonic()
    with pytest.raises(video.VideoFileError) as error:
        with video.open_video_item(
            *mounted_item, cancelled=lambda: time.monotonic() - started > 0.5
        ):
            pytest.fail("cancelled pin")
    assert error.value.reason == "cancelled"
    assert time.monotonic() - started < 2


def test_local_error_and_cancel_close(item, monkeypatch):
    opened = []
    original = os.open

    def track(*args, **kwargs):
        fd = original(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", track)
    with pytest.raises(video.VideoFileError):
        with video.open_video_item(*item, cancelled=lambda: len(opened) > 2):
            pytest.fail("cancelled")
    assert opened
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)
    with pytest.raises(OSError, match="consumer failure"):
        with video.open_video_item(*item) as fd:
            raise OSError("consumer failure")
    with pytest.raises(OSError):
        os.fstat(fd)


@pytest.mark.parametrize("cancel", [False, True])
def test_cleanup_failure_closes_received_fd_preserves_primary(
    mounted_item, monkeypatch, cancel
):
    monkeypatch.setattr(worker, "_video_worker", _healthy_worker)
    received = []
    receive = worker._receive_fd
    cleanup = worker._cleanup_worker

    def capture(*args):
        fd = receive(*args)
        received.append(fd)
        return fd

    def failed_cleanup(process):
        cleanup(process)
        raise SourceUnavailable("private diagnostic")

    monkeypatch.setattr(worker, "_receive_fd", capture)
    monkeypatch.setattr(worker, "_cleanup_worker", failed_cleanup)
    with pytest.raises(video.VideoFileError) as error:
        with video.open_video_item(
            *mounted_item, cancelled=lambda: cancel and bool(received)
        ):
            pytest.fail("cleanup failure must not yield")
    assert error.value.reason == ("cancelled" if cancel else "helper")
    if cancel:
        assert error.value.__notes__ == ["Video file authority failed: helper."]
    with pytest.raises(OSError):
        os.fstat(received[0])


def test_mounted_start_failure(item, monkeypatch):
    from multiprocessing.process import BaseProcess

    def fail_start(process):
        raise OSError("private startup details")

    monkeypatch.setattr(BaseProcess, "start", fail_start)
    with pytest.raises(video.VideoFileError) as error:
        with video.open_video_item(
            item[0], replace(item[1], kind="mounted_directory"), item[2]
        ):
            pytest.fail("failed start")
    assert error.value.reason == "helper"
