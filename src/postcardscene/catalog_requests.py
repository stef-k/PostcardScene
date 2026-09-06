"""DB-only manual refresh requests; no storage probing or reconciliation."""

from postcardscene.catalog import MediaCatalogState
from postcardscene.domain import Source
from postcardscene.filesystem_source import InvalidSource


def request_catalog_reconciliation(database, source_id):
    """Increment a durable coalescing token for an enabled filesystem Source."""
    if type(source_id) is not int or source_id < 1:
        raise InvalidSource("Source identity must be a positive integer.")
    with database.transaction(write=True) as session:
        source = session.get(Source, source_id)
        if (
            source is None
            or not source.enabled
            or source.kind not in ("local_directory", "mounted_directory")
        ):
            raise InvalidSource("An existing enabled filesystem Source is required.")
        state = session.get(MediaCatalogState, source_id)
        if state is None:
            state = MediaCatalogState(source_id=source_id, requested_generation=0)
            session.add(state)
        state.requested_generation += 1
        return state.requested_generation
