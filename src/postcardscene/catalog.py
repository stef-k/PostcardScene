"""Regenerable filesystem inventory beneath Source; row IDs are pagination only."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.orm import Mapped, mapped_column

from postcardscene.filesystem_source import MediaType, validate_relative_path
from postcardscene.persistence import Base

MAX_BATCH_SIZE = 500
ORIENTATIONS = frozenset({"portrait", "landscape", "square"})


class MediaItem(Base):
    __tablename__ = "media_item"
    __table_args__ = (
        UniqueConstraint("source_id", "relative_path", name="uq_media_item_identity"),
        Index("ix_media_item_generation", "source_id", "seen_generation"),
        Index("ix_media_item_selection", "source_id", "media_type", "orientation"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    relative_path: Mapped[str] = mapped_column(String(4096))
    media_type: Mapped[str] = mapped_column(String(16))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    mtime_ns: Mapped[int] = mapped_column(BigInteger)
    seen_generation: Mapped[int] = mapped_column(BigInteger)
    display_width: Mapped[int | None]
    display_height: Mapped[int | None]
    orientation: Mapped[str | None] = mapped_column(String(16))
    metadata_status: Mapped[str | None] = mapped_column(String(16))
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)

    @property
    def identity(self):
        return self.source_id, self.relative_path


class MediaCatalogState(Base):
    __tablename__ = "media_catalog_state"

    source_id: Mapped[int] = mapped_column(
        ForeignKey("source.id", ondelete="CASCADE"), primary_key=True
    )
    requested_generation: Mapped[int] = mapped_column(
        BigInteger,
        CheckConstraint(
            "requested_generation >= 0", name="requested_generation_nonnegative"
        ),
        default=0,
        server_default="0",
    )
    handled_request_generation: Mapped[int] = mapped_column(
        BigInteger,
        CheckConstraint(
            "handled_request_generation >= 0",
            name="handled_request_generation_nonnegative",
        ),
        default=0,
        server_default="0",
    )
    scan_generation: Mapped[int] = mapped_column(BigInteger, default=0)
    completed_generation: Mapped[int] = mapped_column(BigInteger, default=0)
    last_result: Mapped[str] = mapped_column(String(16), default="never_scanned")
    last_attempt_ns: Mapped[int | None] = mapped_column(BigInteger)
    last_success_ns: Mapped[int | None] = mapped_column(BigInteger)

    @property
    def refresh_pending(self):
        return self.requested_generation > self.handled_request_generation

    @property
    def interrupted(self):
        return self.scan_generation != self.completed_generation


def validate_limit(limit):
    if type(limit) is not int or not 1 <= limit <= MAX_BATCH_SIZE:
        raise ValueError(f"Limit must be an integer from 1 to {MAX_BATCH_SIZE}.")


def get_media_item(session, source_id, relative_path):
    validate_relative_path(relative_path)
    return session.scalar(
        select(MediaItem).where(
            MediaItem.source_id == source_id, MediaItem.relative_path == relative_path
        )
    )


def _filters(source_id, media_type, orientation):
    filters = [MediaItem.source_id == source_id]
    if media_type is not None:
        filters.append(MediaItem.media_type == MediaType(media_type).value)
    if orientation is not None:
        if orientation not in ORIENTATIONS:
            raise ValueError("Invalid image orientation.")
        filters.extend(
            (
                MediaItem.media_type == "image",
                MediaItem.orientation == orientation,
                MediaItem.metadata_status == "ready",
            )
        )
    return filters


def list_media_items(
    session, source_id, *, after_id=0, limit=100, media_type=None, orientation=None
):
    """One bounded page; callers use the last row ID only as a continuation."""
    validate_limit(limit)
    if type(after_id) is not int or after_id < 0:
        raise ValueError("Continuation must be a nonnegative integer.")
    return list(
        session.scalars(
            select(MediaItem)
            .where(
                *_filters(source_id, media_type, orientation), MediaItem.id > after_id
            )
            .order_by(MediaItem.id)
            .limit(limit)
        )
    )


def count_media_items(session, source_id, *, media_type=None, orientation=None):
    return session.scalar(
        select(func.count())
        .select_from(MediaItem)
        .where(*_filters(source_id, media_type, orientation))
    )


def catalog_health_counts(session, source_id):
    """Small grouped summary, independent of library size."""
    return {
        (kind, status): count
        for kind, status, count in session.execute(
            select(MediaItem.media_type, MediaItem.metadata_status, func.count())
            .where(MediaItem.source_id == source_id)
            .group_by(MediaItem.media_type, MediaItem.metadata_status)
        )
    }


def supersede_catalog(session, source_id, *, invalidate=False):
    """Call in the Source edit transaction; keep tokens monotonic across edits."""
    state = session.get(MediaCatalogState, source_id)
    if state is not None:
        state.scan_generation += 1
        state.completed_generation = state.scan_generation
        state.handled_request_generation = state.requested_generation
        if invalidate:
            state.last_result = "never_scanned"
            state.last_attempt_ns = state.last_success_ns = None
    if invalidate:
        session.execute(delete(MediaItem).where(MediaItem.source_id == source_id))


def invalidate_catalog(session):
    """Supersede all derived inventory in the stopped restore transaction."""
    session.execute(delete(MediaItem))
    session.execute(
        update(MediaCatalogState).values(
            scan_generation=MediaCatalogState.scan_generation + 1,
            completed_generation=MediaCatalogState.scan_generation + 1,
            handled_request_generation=MediaCatalogState.requested_generation,
            last_result="never_scanned",
            last_attempt_ns=None,
            last_success_ns=None,
        )
    )
