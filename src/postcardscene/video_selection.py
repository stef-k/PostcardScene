"""DB-only video candidates; selection establishes neither file nor decoder authority."""

from dataclasses import dataclass

from postcardscene.catalog import get_media_item, list_media_items
from postcardscene.domain import Source, Widget
from postcardscene.filesystem_source import validate_relative_path


class VideoSelectionError(ValueError):
    """Invalid video semantics or a non-selectable Widget/Source context."""


def validate_video_configuration(widget_kind, configuration):
    """Normalize default-silent audio policy without changing generic domain JSON."""
    if widget_kind != "video":
        raise VideoSelectionError("Widget must be video.")
    if type(configuration) is not dict or configuration.keys() - {
        "audio_enabled",
        "volume",
    }:
        raise VideoSelectionError(
            "Video configuration accepts only optional audio_enabled and volume."
        )
    audio_enabled = configuration.get("audio_enabled", False)
    volume = configuration.get("volume", 50)
    if type(audio_enabled) is not bool:
        raise VideoSelectionError("Audio enabled must be a boolean.")
    if type(volume) is not int or not 0 <= volume <= 100:
        raise VideoSelectionError("Volume must be an integer from 0 to 100.")
    return {"audio_enabled": audio_enabled, "volume": volume}


@dataclass(frozen=True)
class VideoSelectionContext:
    source_id: int
    audio_enabled: bool
    volume: int


def resolve_video_context(session, widget_id):
    """Resolve in the caller's short transaction without probing Source storage."""
    widget = session.get(Widget, widget_id)
    if widget is None or not widget.enabled:
        raise VideoSelectionError("Video Widget is missing or disabled.")
    configuration = validate_video_configuration(widget.kind, widget.configuration)
    source = session.get(Source, widget.source_id) if widget.source_id else None
    if source is None or not source.enabled:
        raise VideoSelectionError("Video Source is missing or disabled.")
    if source.kind not in ("local_directory", "mounted_directory"):
        raise VideoSelectionError("Video Source must be a filesystem Source.")
    return VideoSelectionContext(source.id, **configuration)


def _validate_identity(source_id, relative_path):
    validate_relative_path(relative_path)
    if type(source_id) is not int or source_id < 1:
        raise VideoSelectionError("Source identity must be a positive integer.")


@dataclass(frozen=True)
class SelectedVideo:
    """Canonical identity and expected freshness, never permission to open a file."""

    source_id: int
    relative_path: str
    size_bytes: int
    mtime_ns: int

    def __post_init__(self):
        _validate_identity(self.source_id, self.relative_path)
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise VideoSelectionError("Size must be a nonnegative integer.")
        if type(self.mtime_ns) is not int:
            raise VideoSelectionError("Modification freshness must be an integer.")

    @property
    def identity(self):
        return self.source_id, self.relative_path


def _snapshot(item):
    return SelectedVideo(
        item.source_id, item.relative_path, item.size_bytes, item.mtime_ns
    )


def get_selected_video(session, widget_id, source_id, relative_path):
    """Canonical lookup; absent/ineligible/mismatched media returns None."""
    context = resolve_video_context(session, widget_id)
    _validate_identity(source_id, relative_path)
    if source_id != context.source_id:
        return None
    item = get_media_item(session, source_id, relative_path)
    return _snapshot(item) if item is not None and item.media_type == "video" else None


def list_selected_videos(session, widget_id, *, after_id=0, limit=100):
    """Return (immutable page, last row ID), or ((), None) at exhaustion.

    Row ID is storage continuation only, never playback order or media identity.
    Revalidate Widget/Source context each call; load at most 500 catalog rows.
    """
    context = resolve_video_context(session, widget_id)
    rows = list_media_items(
        session, context.source_id, after_id=after_id, limit=limit, media_type="video"
    )
    return tuple(_snapshot(item) for item in rows), rows[-1].id if rows else None
