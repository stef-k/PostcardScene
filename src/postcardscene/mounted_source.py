"""Disposable spawned operations for already-mounted Linux NFS/SMB sources."""

import math
import multiprocessing
import re
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from postcardscene.filesystem_source import (
    EnumerationFailed,
    InvalidSource,
    MediaEntry,
    PathPolicy,
    SourceStatus,
    SourceUnavailable,
    _check_cancelled,
    _enumerate_directory,
    _source_directory,
)
from postcardscene.image_metadata import ImageMetadata, inspect_image

DEFAULT_MOUNT_TIMEOUT = 10.0
_POLL_SECONDS = 0.02
_CLEANUP_SECONDS = 0.1


def _network_mount_covers(path: Path, mountinfo: str) -> bool:
    """Select the deepest component-containing mount, including local overlays."""
    covering = []
    for line in mountinfo.splitlines():
        fields, separator, filesystem = line.partition(" - ")
        columns = fields.split()
        if not separator or len(columns) < 6 or not filesystem.split():
            raise SourceUnavailable("Mount information is unavailable.")
        mountpoint = Path(
            re.sub(
                r"\\(040|011|012|134)",
                lambda match: chr(int(match[1], 8)),
                columns[4],
            )
        )
        if path.is_relative_to(mountpoint):
            covering.append((len(mountpoint.parts), filesystem.split()[0]))
    if not covering:
        return False
    # Later equal-depth entries describe an overmount at the same path.
    deepest = max(range(len(covering)), key=lambda i: (covering[i][0], i))
    return covering[deepest][1] in {"nfs", "nfs4", "cifs"}


def _mounted_directory(configuration: object, policy: PathPolicy) -> Path:
    root = _source_directory("mounted_directory", configuration, policy)
    mountinfo = Path("/proc/self/mountinfo").read_text()
    if not _network_mount_covers(root, mountinfo):
        raise SourceUnavailable("Source is not on an available network mount.")
    return root


def _send(connection, kind, value=None):
    try:
        connection.send((kind, value))
    except OSError:
        # Early parent close is ordinary cancellation, not a child traceback.
        raise SystemExit from None


def _mounted_worker(connection, configuration, policy, enumerate_entries):
    """Only this child touches Source storage; IPC carries no raw diagnostics."""
    try:
        _mounted_directory(configuration, policy)
        _send(connection, "progress")
        if isinstance(enumerate_entries, tuple):
            for entry in enumerate_entries:
                _mounted_directory(configuration, policy)
                _send(connection, "progress")
                result = inspect_image(
                    "mounted_directory", configuration, policy, entry
                )
                _mounted_directory(configuration, policy)
                _send(connection, "metadata", result)
        elif enumerate_entries:
            for entry in _enumerate_directory(
                "mounted_directory",
                configuration,
                policy,
                progress=lambda: _send(connection, "progress"),
            ):
                _send(connection, "entry", entry)
        # An unmount during traversal must not authorize catalog deletion.
        _mounted_directory(configuration, policy)
        _send(connection, "success")
    except InvalidSource:
        _send(connection, "invalid")
    except (SourceUnavailable, OSError, RuntimeError):
        _send(connection, "unavailable")
    except Exception:
        _send(connection, "failure")
    finally:
        connection.close()


def _validate_timeout(timeout_seconds: float) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("Mount timeout must be a finite positive number.")


def _cleanup_worker(process) -> None:
    if process.pid is None:
        return
    if process.is_alive():
        process.terminate()
    process.join(_CLEANUP_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(_CLEANUP_SECONDS)
    if process.is_alive():
        raise SourceUnavailable("Mounted worker could not be stopped.")
    process.close()


def _receive_entries(connection, cancelled, timeout_seconds, enumerate_entries):
    deadline = time.monotonic() + timeout_seconds
    while True:
        _check_cancelled(cancelled)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SourceUnavailable("Mounted source stopped making progress.")
        try:
            if not connection.poll(min(_POLL_SECONDS, remaining)):
                continue
            message = connection.recv()
        except Exception:
            raise EnumerationFailed("Mounted worker ended without success.") from None
        _check_cancelled(cancelled)
        if not isinstance(message, tuple) or len(message) != 2:
            raise EnumerationFailed("Mounted worker protocol failed.")
        kind, value = message
        if (
            kind == "entry"
            and enumerate_entries is True
            and isinstance(value, MediaEntry)
        ):
            yield value
        elif (
            kind == "metadata"
            and isinstance(enumerate_entries, tuple)
            and isinstance(value, ImageMetadata)
        ):
            yield value
        elif value is not None:
            raise EnumerationFailed("Mounted worker protocol failed.")
        elif kind == "success":
            return
        elif kind == "invalid":
            raise InvalidSource("Mounted Source configuration or authority is invalid.")
        elif kind == "unavailable":
            raise SourceUnavailable("Mounted source is unavailable.")
        elif kind != "progress":
            raise EnumerationFailed("Mounted source enumeration was incomplete.")
        # Consumer processing time is not a worker progress failure.
        deadline = time.monotonic() + timeout_seconds


def _mounted_operation(
    configuration, policy, cancelled, timeout_seconds, enumerate_entries
):
    _validate_timeout(timeout_seconds)
    _check_cancelled(cancelled)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_mounted_worker,
        args=(sender, configuration, policy, enumerate_entries),
    )
    try:
        try:
            process.start()
        except Exception:
            raise EnumerationFailed("Mounted worker could not start.") from None
        sender.close()
        yield from _receive_entries(
            receiver, cancelled, timeout_seconds, enumerate_entries
        )
    finally:
        receiver.close()
        sender.close()
        primary_error = sys.exception()
        try:
            _cleanup_worker(process)
        except SourceUnavailable as cleanup_error:
            if primary_error is None:
                raise
            # Preserve cancellation/failure classification if kernel cleanup fails.
            primary_error.add_note(str(cleanup_error))


def mounted_source_status(
    configuration: object,
    policy: PathPolicy,
    *,
    timeout_seconds: float = DEFAULT_MOUNT_TIMEOUT,
) -> SourceStatus:
    """Fresh isolated probe; no mounted-path operations run in the caller."""
    try:
        for _ in _mounted_operation(
            configuration, policy, None, timeout_seconds, False
        ):
            pass
    except InvalidSource:
        return SourceStatus.INVALID
    except (SourceUnavailable, EnumerationFailed):
        return SourceStatus.UNAVAILABLE
    return SourceStatus.AVAILABLE


def enumerate_mounted_directory(
    configuration: object,
    policy: PathPolicy,
    *,
    cancelled: Callable[[], bool] | None = None,
    timeout_seconds: float = DEFAULT_MOUNT_TIMEOUT,
) -> Iterator[MediaEntry]:
    """Stream shared candidates; only explicit child success permits exhaustion.

    Timeout measures lack of progress, not total duration. Close this iterator
    when abandoning consumption. Every operation owns a fresh disposable child.
    """
    yield from _mounted_operation(
        configuration, policy, cancelled, timeout_seconds, True
    )


def inspect_mounted_images(
    configuration,
    policy,
    entries,
    *,
    cancelled=None,
    timeout_seconds=DEFAULT_MOUNT_TIMEOUT,
):
    """Inspect at most 64 pending images through the existing spawn lifecycle."""
    if not isinstance(entries, (list, tuple)) or not 1 <= len(entries) <= 64:
        raise ValueError("Image metadata batch must contain 1 to 64 entries.")
    if any(
        not isinstance(entry, MediaEntry) or entry.media_type != "image"
        for entry in entries
    ):
        raise ValueError("Image metadata batches require image MediaEntries.")
    yield from _mounted_operation(
        configuration, policy, cancelled, timeout_seconds, tuple(entries)
    )


def _check_open_mount(stream):
    """Also check the pinned file's mount, closing unmount/open fallthrough races."""
    info = Path(f"/proc/self/fdinfo/{stream.fileno()}").read_text()
    mount_id = next(
        line.split()[1] for line in info.splitlines() if line.startswith("mnt_id:")
    )
    mounts = Path("/proc/self/mountinfo").read_text()
    entry = next(
        (line for line in mounts.splitlines() if line.split()[0] == mount_id), ""
    )
    if not entry or not _network_mount_covers(Path("/"), _root_mount_entry(entry)):
        raise SourceUnavailable("Image mount is unavailable.")


def _root_mount_entry(entry):
    # Reuse the existing mount parser/type contract for this exact pinned mount.
    fields = entry.split()
    fields[4] = "/"
    return " ".join(fields)
