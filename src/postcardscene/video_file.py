"""Fresh Source authority and context-owned video FD; no player or media decoding."""

import os
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from enum import StrEnum

from postcardscene.domain import Source
from postcardscene.filesystem_source import (
    InvalidSource,
    PathPolicy,
    SourceUnavailable,
    _absolute_path,
    _open_media_item,
)
from postcardscene.mounted_source import _validate_timeout
from postcardscene.video_selection import SelectedVideo


class VideoFileFailure(StrEnum):
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    HELPER = "helper"


class VideoFileError(Exception):
    """Public diagnostics contain only a stable reason, never storage details."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(f"Video file authority failed: {reason.value}.")


@dataclass(frozen=True)
class VideoSource:
    source_id: int
    kind: str
    path: str
    recursive: bool

    @property
    def configuration(self):
        return {"path": self.path, "recursive": self.recursive}


def _validate_context(selected, source):
    try:
        if not isinstance(selected, SelectedVideo):
            raise ValueError
        selected.__post_init__()
        if (
            not isinstance(source, VideoSource)
            or type(source.source_id) is not int
            or source.source_id != selected.source_id
            or source.kind not in ("local_directory", "mounted_directory")
            or type(source.recursive) is not bool
        ):
            raise ValueError
        _absolute_path(source.path)  # Lexical only; mounted I/O belongs to the child.
    except (ValueError, InvalidSource):
        raise VideoFileError(VideoFileFailure.INVALID) from None


def capture_video_source(session, selected):
    """Read current enabled authority in a short transaction immediately before pin."""
    if not isinstance(selected, SelectedVideo):
        raise VideoFileError(VideoFileFailure.INVALID)
    source = session.get(Source, selected.source_id)
    if source is None or not source.enabled:
        raise VideoFileError(VideoFileFailure.INVALID)
    config = source.configuration
    if type(config) is not dict or set(config) != {"path", "recursive"}:
        raise VideoFileError(VideoFileFailure.INVALID)
    snapshot = VideoSource(source.id, source.kind, config["path"], config["recursive"])
    _validate_context(selected, snapshot)
    return snapshot


def _check_cancelled(cancelled):
    if cancelled is not None and cancelled():
        raise VideoFileError(VideoFileFailure.CANCELLED)


@contextmanager
def _open_selected(selected, source, policy, progress):
    with ExitStack() as stack:
        try:
            stream = stack.enter_context(
                _open_media_item(
                    source.kind,
                    source.configuration,
                    policy,
                    selected.relative_path,
                    selected.size_bytes,
                    selected.mtime_ns,
                    progress=progress,
                )
            )
        except InvalidSource:
            raise VideoFileError(VideoFileFailure.INVALID) from None
        except (SourceUnavailable, OSError, RuntimeError):
            raise VideoFileError(VideoFileFailure.UNAVAILABLE) from None
        yield stream


@contextmanager
def open_video_item(selected, source, policy, *, cancelled=None, timeout_seconds=10.0):
    """Yield a borrowed read-only FD valid only inside this context.

    Capture Source immediately beforehand, outside any storage-spanning transaction.
    Trusted future player plumbing may use pass_fds and fd://N; it must retire its
    child before context exit. Never close this borrowed FD or publish/persist it.
    Pinning survives pathname replacement, not in-place writes by external owners.
    """
    _validate_context(selected, source)
    try:
        _validate_timeout(timeout_seconds)
        if not isinstance(policy, PathPolicy):
            raise ValueError
    except ValueError:
        raise VideoFileError(VideoFileFailure.INVALID) from None
    _check_cancelled(cancelled)
    if source.kind == "mounted_directory":
        from postcardscene.video_file_worker import pin_mounted_video

        descriptor = pin_mounted_video(
            selected, source, policy, cancelled, timeout_seconds
        )
        try:
            _check_cancelled(cancelled)
            yield descriptor
        finally:
            os.close(descriptor)
    else:
        with _open_selected(
            selected, source, policy, lambda: _check_cancelled(cancelled)
        ) as stream:
            _check_cancelled(cancelled)
            yield stream.fileno()
