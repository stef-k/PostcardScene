"""Persisted catalog presentation; no live storage or runtime status inference."""

from datetime import datetime

from postcardscene.catalog import MediaCatalogState, catalog_health_counts

KINDS = {
    "local_directory": "Local directory",
    "mounted_directory": "Mounted network directory",
}
RESULTS = {
    "never_scanned": ("Never scanned", "No authoritative catalog build yet."),
    "ready": ("Ready", "Last presence reconciliation completed authoritatively."),
    "unavailable": (
        "Source unavailable",
        "Latest attempt could not authoritatively traverse storage.",
    ),
    "error": ("Needs attention", "Latest attempt failed non-authoritatively."),
    "cancelled": (
        "Cancelled",
        "Latest attempt was cancelled; it is not authoritative.",
    ),
}


def health(source, state):
    if not source.enabled:
        return (
            "Disabled",
            "Catalog knowledge is preserved while this Source is inactive.",
        )
    if state is not None and state.interrupted:
        return (
            "Refreshing / interrupted",
            "An attempt has not completed; it may still be running or was interrupted.",
        )
    if state is not None and state.refresh_pending:
        return (
            "Refresh queued",
            "Waiting for the runtime to consume the refresh request.",
        )
    return RESULTS.get(
        state.last_result if state else "never_scanned", RESULTS["error"]
    )


def timestamp(nanoseconds, timezone):
    if nanoseconds is None:
        return "Not yet recorded"
    return datetime.fromtimestamp(nanoseconds // 1_000_000_000, timezone).strftime(
        "%Y-%m-%d %H:%M:%S %Z (UTC%z)"
    )


def source_view(session, source, timezone):
    state = session.get(MediaCatalogState, source.id)
    grouped = catalog_health_counts(session, source.id)
    label, description = health(source, state)
    counts = {
        "Total cataloged items": sum(grouped.values()),
        "Images total": sum(n for (kind, _), n in grouped.items() if kind == "image"),
        "Image metadata ready": grouped.get(("image", "ready"), 0),
        "Image metadata pending": grouped.get(("image", "pending"), 0),
        "Image metadata error": grouped.get(("image", "error"), 0),
        "Videos total": sum(n for (kind, _), n in grouped.items() if kind == "video"),
    }
    return dict(
        id=source.id,
        name=source.name,
        kind=KINDS[source.kind],
        path=source.configuration.get("path", ""),
        recursive=source.configuration.get("recursive", False),
        enabled=source.enabled,
        health=label,
        description=description,
        last_result=RESULTS.get(state.last_result, RESULTS["error"])[0]
        if state
        else None,
        last_attempt=timestamp(state.last_attempt_ns if state else None, timezone),
        last_success=timestamp(state.last_success_ns if state else None, timezone),
        counts=counts,
    )
