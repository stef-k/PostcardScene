"""Bounded DB selection for transient Widget streams; no filesystem authority."""

from sqlalchemy import func, select

from postcardscene import image_selection as images
from postcardscene import video_selection as videos
from postcardscene.catalog import MediaItem


def _selection(session, widget_id, kind):
    if kind in images.IMAGE_WIDGET_KINDS:
        context = images.resolve_image_context(session, widget_id)
        if context.widget_kind != kind:
            raise images.ImageSelectionError("Widget kind changed during selection.")
        return context, images._eligible_images(context.source_id), images._snapshot
    if kind == "video":
        context = videos.resolve_video_context(session, widget_id)
        query = select(MediaItem).where(
            MediaItem.source_id == context.source_id, MediaItem.media_type == "video"
        )
        return context, query, videos._snapshot
    raise ValueError("Unsupported media stream kind.")


def select_candidate(
    database, widget_id, kind, *, after=None, recent=(), random_fraction=None
):
    """Return one frozen candidate, revalidating context in a short transaction.

    Ordered mode uses relative-path keysets and wraps once. Shuffle accepts an
    already drawn fraction in [0, 1), so no transaction spans RNG computation.
    COUNT plus canonical rank may scan eligible index entries in SQLite, but
    returns only one catalog row and at most 32 excluded paths to Python.
    Relax exhausted exclusions oldest-first, preserving the latest if possible.
    """
    if len(recent) > 32:
        raise ValueError("Recent exclusion is limited to 32 identities.")
    if random_fraction is not None and not 0 <= random_fraction < 1:
        raise ValueError("Random fraction must be in [0, 1).")
    with database.transaction() as session:
        context, query, snapshot = _selection(session, widget_id, kind)
        if random_fraction is None:
            ordered = query.order_by(MediaItem.relative_path)
            item = None
            if after is not None and after[0] == context.source_id:
                item = session.scalar(
                    ordered.where(MediaItem.relative_path > after[1]).limit(1)
                )
            if item is None:
                item = session.scalar(ordered.limit(1))
        else:
            count = session.scalar(
                query.with_only_columns(func.count()).select_from(MediaItem)
            )
            if not count:
                return None
            paths = list(
                dict.fromkeys(
                    path
                    for source, path in reversed(recent)
                    if source == context.source_id
                )
            )[::-1]
            excluded = (
                set(
                    session.scalars(
                        query.with_only_columns(MediaItem.relative_path)
                        .where(MediaItem.relative_path.in_(paths))
                        .limit(32)
                    )
                )
                if paths
                else set()
            )
            for path in paths:
                if len(excluded) < count:
                    break
                excluded.discard(path)
            available = count - len(excluded)
            item = session.scalar(
                query.where(MediaItem.relative_path.not_in(excluded))
                .order_by(MediaItem.relative_path)
                .offset(int(random_fraction * available))
                .limit(1)
            )
        return snapshot(item) if item is not None else None


def resolve_candidate(database, widget_id, kind, identity):
    """Revalidate a retained/history identity without reading original media."""
    with database.transaction() as session:
        _selection(session, widget_id, kind)
        getter = (
            images.get_selected_image
            if kind in images.IMAGE_WIDGET_KINDS
            else videos.get_selected_video
        )
        return getter(session, widget_id, *identity)
