"""DB-only, immutable authority for a single disposable image delivery."""

from dataclasses import dataclass
from enum import StrEnum

from postcardscene.domain import Source
from postcardscene.filesystem_source import InvalidSource, _absolute_path
from postcardscene.image_selection import ImageFrame, SelectedImage


class FrameFailure(StrEnum):
    INVALID = "invalid"
    UNSAFE = "unsafe"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    HELPER = "helper"
    PRESENTATION = "presentation"
    CANCELLED = "cancelled"


class ImageDeliveryError(Exception):
    """Safe classification; never include paths, tokens or child diagnostics."""

    def __init__(self, reason: FrameFailure):
        self.reason = reason
        super().__init__(f"Image frame delivery failed: {reason.value}.")


@dataclass(frozen=True)
class ImageSource:
    source_id: int
    kind: str
    path: str
    recursive: bool

    @property
    def configuration(self):
        return {"path": self.path, "recursive": self.recursive}


def validate_frame(frame):
    if (
        not isinstance(frame, ImageFrame)
        or type(frame.images) is not tuple
        or not 1 <= len(frame.images) <= 2
        or frame.fit not in ("contain", "cover")
        or any(not isinstance(image, SelectedImage) for image in frame.images)
    ):
        raise ImageDeliveryError(FrameFailure.INVALID)
    if len({image.source_id for image in frame.images}) != 1 or len(
        {image.identity for image in frame.images}
    ) != len(frame.images):
        raise ImageDeliveryError(FrameFailure.INVALID)


def validate_context(frame, source):
    validate_frame(frame)
    if (
        not isinstance(source, ImageSource)
        or source.source_id != frame.images[0].source_id
        or source.kind not in ("local_directory", "mounted_directory")
        or type(source.recursive) is not bool
    ):
        raise ImageDeliveryError(FrameFailure.INVALID)
    try:
        _absolute_path(source.path)  # Lexical only: no parent-side filesystem I/O.
    except InvalidSource:
        raise ImageDeliveryError(FrameFailure.INVALID) from None


def capture_image_source(session, frame):
    """Capture within a short transaction immediately before launch; no path I/O."""
    validate_frame(frame)
    source = session.get(Source, frame.images[0].source_id)
    if source is None or not source.enabled:
        raise ImageDeliveryError(FrameFailure.INVALID)
    config = source.configuration
    if type(config) is not dict or set(config) != {"path", "recursive"}:
        raise ImageDeliveryError(FrameFailure.INVALID)
    snapshot = ImageSource(source.id, source.kind, config["path"], config["recursive"])
    validate_context(frame, snapshot)
    return snapshot
