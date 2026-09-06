"""Bounded presence reconciliation, followed by independently fallible metadata."""

import time
from contextlib import closing
from copy import deepcopy

from sqlalchemy import delete, select

from postcardscene.catalog import (
    MediaCatalogState,
    MediaItem,
    get_media_item,
    validate_limit,
)
from postcardscene.domain import Source
from postcardscene.filesystem_source import (
    EnumerationFailed,
    InvalidSource,
    MediaEntry,
    ScanCancelled,
    SourceUnavailable,
    _check_cancelled,
    enumerate_local_directory,
)
from postcardscene.image_metadata import inspect_image
from postcardscene.mounted_source import (
    DEFAULT_MOUNT_TIMEOUT,
    enumerate_mounted_directory,
    inspect_mounted_images,
)


class ScanSuperseded(ScanCancelled):
    """A newer attempt owns this Source; the old attempt must stop writing."""


def _start(database, source_id, request_generation=None):
    with database.transaction(write=True) as session:
        source = session.get(Source, source_id)
        if source is None or source.kind not in (
            "local_directory",
            "mounted_directory",
        ):
            raise InvalidSource("An existing filesystem Source is required.")
        if not source.enabled:
            raise InvalidSource("Disabled Sources are not reconciled.")
        state = session.get(MediaCatalogState, source_id)
        if request_generation is not None and (
            state is None
            or state.handled_request_generation >= request_generation
            or state.requested_generation < request_generation
        ):
            raise ScanSuperseded("Refresh request was superseded before execution.")
        if state is None:
            state = MediaCatalogState(source_id=source_id, scan_generation=0)
            session.add(state)
        state.scan_generation += 1
        state.last_attempt_ns = time.time_ns()
        return state.scan_generation, source.kind, deepcopy(source.configuration)


def _current(session, source_id, generation):
    state = session.get(MediaCatalogState, source_id)
    if state is None or state.scan_generation != generation:
        raise ScanSuperseded("Reconciliation was superseded.")
    return state


def _observe(database, source_id, generation, entries):
    with database.transaction(write=True) as session:
        _current(session, source_id, generation)
        for entry in entries:
            if len(entry.relative_path.encode("utf-8")) > 4096:
                raise InvalidSource("Media identity exceeds Linux path bounds.")
            row = get_media_item(session, source_id, entry.relative_path)
            if row is None:
                row = MediaItem(source_id=source_id, relative_path=entry.relative_path)
                session.add(row)
            changed = (row.media_type, row.size_bytes, row.mtime_ns) != (
                entry.media_type,
                entry.size_bytes,
                entry.mtime_ns,
            )
            row.media_type = entry.media_type.value
            row.size_bytes = entry.size_bytes
            row.mtime_ns = entry.mtime_ns
            row.seen_generation = generation
            if changed:
                row.display_width = row.display_height = row.orientation = None
                row.duration_ms = None
                row.metadata_status = "pending" if entry.media_type == "image" else None
            # Enumerator identities are unique; flush also handles repeated entries
            # without accumulating duplicate transient rows in this batch.
            session.flush()


def _finish(database, source_id, generation, result):
    with database.transaction(write=True) as session:
        state = _current(session, source_id, generation)
        state.completed_generation = generation
        state.last_result = result
        if result == "ready":
            state.last_success_ns = time.time_ns()


def _remove_absent(database, source_id, generation, batch_size, cancelled):
    while True:
        _check_cancelled(cancelled)
        with database.transaction(write=True) as session:
            _current(session, source_id, generation)
            ids = list(
                session.scalars(
                    select(MediaItem.id)
                    .where(
                        MediaItem.source_id == source_id,
                        MediaItem.seen_generation != generation,
                    )
                    .order_by(MediaItem.id)
                    .limit(batch_size)
                )
            )
            if not ids:
                return
            session.execute(delete(MediaItem).where(MediaItem.id.in_(ids)))


def _presence(database, source_id, generation, entries, batch_size, cancelled):
    batch = []
    with closing(entries):
        for entry in entries:
            _check_cancelled(cancelled)
            batch.append(entry)
            if len(batch) == batch_size:
                _observe(database, source_id, generation, batch)
                batch.clear()
        _check_cancelled(cancelled)
        if batch:
            _observe(database, source_id, generation, batch)
    _remove_absent(database, source_id, generation, batch_size, cancelled)
    _finish(database, source_id, generation, "ready")


def _pending(database, source_id, generation, after_id, batch_size):
    with database.transaction() as session:
        _current(session, source_id, generation)
        rows = session.scalars(
            select(MediaItem)
            .where(
                MediaItem.source_id == source_id,
                MediaItem.id > after_id,
                MediaItem.media_type == "image",
                MediaItem.metadata_status.in_(("pending", "error")),
            )
            .order_by(MediaItem.id)
            .limit(batch_size)
        )
        return [
            (
                row.id,
                MediaEntry(
                    row.relative_path, row.media_type, row.size_bytes, row.mtime_ns
                ),
            )
            for row in rows
        ]


def _save_metadata(database, source_id, generation, result):
    with database.transaction(write=True) as session:
        _current(session, source_id, generation)
        row = get_media_item(session, source_id, result.entry.relative_path)
        if row is None or (row.media_type, row.size_bytes, row.mtime_ns) != (
            "image",
            result.entry.size_bytes,
            result.entry.mtime_ns,
        ):
            return
        row.metadata_status = result.status
        row.display_width = result.width
        row.display_height = result.height
        row.orientation = result.orientation


def _local_metadata(kind, configuration, policy, entries, cancelled):
    for entry in entries:
        _check_cancelled(cancelled)
        yield inspect_image(kind, configuration, policy, entry)


def _metadata(
    database,
    source_id,
    generation,
    kind,
    configuration,
    policy,
    batch_size,
    cancelled,
    timeout,
):
    after_id = 0
    while True:
        _check_cancelled(cancelled)
        pending = _pending(database, source_id, generation, after_id, batch_size)
        if not pending:
            return
        after_id = pending[-1][0]
        entries = [entry for _, entry in pending]
        if kind == "mounted_directory":
            results = inspect_mounted_images(
                configuration,
                policy,
                entries,
                cancelled=cancelled,
                timeout_seconds=timeout,
            )
        else:
            results = _local_metadata(kind, configuration, policy, entries, cancelled)
        with closing(results):
            for result in results:
                _check_cancelled(cancelled)
                _save_metadata(database, source_id, generation, result)


def reconcile_filesystem_source(
    database,
    source_id,
    policy,
    *,
    cancelled=None,
    request_generation=None,
    batch_size=100,
    metadata_batch_size=32,
    mounted_timeout_seconds=DEFAULT_MOUNT_TIMEOUT,
):
    """Reconcile once; caller owns cadence and serializes normal per-Source work.

    Disabled/missing/non-filesystem Sources raise InvalidSource without mutation.
    Presence failures record handled state then propagate. Metadata worker failures
    propagate too, but retain the already committed ready presence result. Abrupt
    termination leaves scan/completed generation mismatch as interruption evidence.
    """
    validate_limit(batch_size)
    validate_limit(metadata_batch_size)
    if metadata_batch_size > 64:
        raise ValueError("Metadata batch size cannot exceed 64.")
    _check_cancelled(cancelled)
    generation, kind, configuration = _start(database, source_id, request_generation)
    try:
        if kind == "local_directory":
            entries = enumerate_local_directory(
                configuration, policy, cancelled=cancelled
            )
        else:
            entries = enumerate_mounted_directory(
                configuration,
                policy,
                cancelled=cancelled,
                timeout_seconds=mounted_timeout_seconds,
            )
        _presence(database, source_id, generation, entries, batch_size, cancelled)
    except Exception as error:
        result = (
            "cancelled"
            if isinstance(error, ScanCancelled)
            else "unavailable"
            if isinstance(error, SourceUnavailable)
            else "error"
        )
        try:
            _finish(database, source_id, generation, result)
        except ScanSuperseded:
            pass
        except Exception:
            error.add_note(
                "Could not persist reconciliation completion; attempt may remain interrupted."
            )
        if isinstance(
            error, (InvalidSource, EnumerationFailed, ScanCancelled, SourceUnavailable)
        ):
            raise
        raise EnumerationFailed("Catalog reconciliation failed.") from error
    _metadata(
        database,
        source_id,
        generation,
        kind,
        configuration,
        policy,
        metadata_batch_size,
        cancelled,
        mounted_timeout_seconds,
    )
