"""DB-only image eligibility and stateless grouping of runtime-supplied candidates.

Catalog readiness does not establish Chromium decode support. No selection here
provides filesystem authority; consumers must re-resolve Source paths at use.
"""

from dataclasses import dataclass

from sqlalchemy import select

from postcardscene.catalog import ORIENTATIONS, MediaItem, validate_limit
from postcardscene.domain import Source, Widget
from postcardscene.filesystem_source import validate_relative_path

IMAGE_WIDGET_KINDS = frozenset({"image", "portrait_image_pair"})


class ImageSelectionError(ValueError):
    """Invalid image semantics or a non-selectable Widget/Source context."""


def validate_image_configuration(widget_kind, configuration):
    """Normalize the only V0 image option; generic domain JSON stays generic."""
    if not isinstance(widget_kind, str) or widget_kind not in IMAGE_WIDGET_KINDS:
        raise ImageSelectionError("Widget must be image or portrait_image_pair.")
    if type(configuration) is not dict or configuration.keys() - {"fit"}:
        raise ImageSelectionError("Image configuration accepts only optional fit.")
    fit = configuration.get("fit", "contain")
    if fit not in ("contain", "cover"):
        raise ImageSelectionError("Image fit must be contain or cover.")
    return {"fit": fit}


@dataclass(frozen=True)
class ImageSelectionContext:
    source_id: int
    widget_kind: str
    fit: str


def resolve_image_context(session, widget_id):
    """Resolve in the caller's short transaction, without probing Source storage."""
    widget = session.get(Widget, widget_id)
    if widget is None or not widget.enabled:
        raise ImageSelectionError("Image Widget is missing or disabled.")
    configuration = validate_image_configuration(widget.kind, widget.configuration)
    source = session.get(Source, widget.source_id) if widget.source_id else None
    if source is None or not source.enabled:
        raise ImageSelectionError("Image Source is missing or disabled.")
    if source.kind not in ("local_directory", "mounted_directory"):
        raise ImageSelectionError("Image Source must be a filesystem Source.")
    return ImageSelectionContext(source.id, widget.kind, configuration["fit"])


@dataclass(frozen=True)
class SelectedImage:
    """Canonical identity, expected freshness and normalized presentation metadata."""

    source_id: int
    relative_path: str
    size_bytes: int
    mtime_ns: int
    display_width: int
    display_height: int
    orientation: str

    def __post_init__(self):
        validate_relative_path(self.relative_path)
        if type(self.source_id) is not int or self.source_id < 1:
            raise ImageSelectionError("Source identity must be a positive integer.")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ImageSelectionError("Size must be a nonnegative integer.")
        if type(self.mtime_ns) is not int:
            raise ImageSelectionError("Modification freshness must be an integer.")
        if any(
            type(value) is not int or value <= 0
            for value in (self.display_width, self.display_height)
        ):
            raise ImageSelectionError(
                "Presentation dimensions must be positive integers."
            )
        if (
            not isinstance(self.orientation, str)
            or self.orientation not in ORIENTATIONS
        ):
            raise ImageSelectionError("Invalid normalized image orientation.")

    @property
    def identity(self):
        return self.source_id, self.relative_path


def _eligible_images(source_id):
    return select(MediaItem).where(
        MediaItem.source_id == source_id,
        MediaItem.media_type == "image",
        MediaItem.metadata_status == "ready",
        MediaItem.display_width > 0,
        MediaItem.display_height > 0,
        MediaItem.orientation.in_(ORIENTATIONS),
    )


def _snapshot(item):
    return SelectedImage(
        item.source_id,
        item.relative_path,
        item.size_bytes,
        item.mtime_ns,
        item.display_width,
        item.display_height,
        item.orientation,
    )


def get_selected_image(session, widget_id, source_id, relative_path):
    """Canonical lookup; absent/ineligible/mismatched media returns None."""
    context = resolve_image_context(session, widget_id)
    validate_relative_path(relative_path)
    if source_id != context.source_id:
        return None
    item = session.scalar(
        _eligible_images(source_id).where(MediaItem.relative_path == relative_path)
    )
    return _snapshot(item) if item is not None else None


def list_selected_images(session, widget_id, *, after_id=0, limit=100):
    """Return (immutable page, last row ID), or ((), None) at exhaustion.

    Row ID is a storage continuation only, never playback order or media identity.
    Each call revalidates Widget/Source context in the caller's transaction.
    """
    validate_limit(limit)
    if type(after_id) is not int or after_id < 0:
        raise ImageSelectionError("Continuation must be a nonnegative integer.")
    context = resolve_image_context(session, widget_id)
    # The existing Source/type/orientation index ends in rowid. Page each of
    # its three orientation ranges, then merge at most 3 * limit rows; a single
    # IN query would sort the entire remaining Source selection in SQLite.
    rows = []
    for orientation in sorted(ORIENTATIONS):
        rows.extend(
            session.scalars(
                _eligible_images(context.source_id)
                .where(MediaItem.orientation == orientation, MediaItem.id > after_id)
                .order_by(MediaItem.id)
                .limit(limit)
            )
        )
    rows = sorted(rows, key=lambda item: item.id)[:limit]
    return tuple(_snapshot(item) for item in rows), rows[-1].id if rows else None


@dataclass(frozen=True)
class ImageFrame:
    images: tuple[SelectedImage, ...]
    fit: str


def build_image_frame(
    widget_kind, fit, first: SelectedImage, lookahead: SelectedImage | None = None
):
    """Group caller-owned first/lookahead; runtime advances by consumed count."""
    fit = validate_image_configuration(widget_kind, {"fit": fit})["fit"]
    if not isinstance(first, SelectedImage):
        raise ImageSelectionError("First candidate must be a selected image.")
    pair = (
        widget_kind == "portrait_image_pair"
        and first.orientation == "portrait"
        and isinstance(lookahead, SelectedImage)
        and lookahead.orientation == "portrait"
        and first.identity != lookahead.identity
    )
    candidates = (first, lookahead) if pair else (first,)
    return ImageFrame(candidates, fit), len(candidates)
