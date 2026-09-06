"""One serial catalog request consumer owned by the runtime process."""

import logging
from threading import Thread

from sqlalchemy import select

from postcardscene.catalog import MediaCatalogState
from postcardscene.catalog_reconciliation import reconcile_filesystem_source
from postcardscene.filesystem_source import FilesystemSourceError, ScanCancelled

POLL_SECONDS = 1.0
JOIN_SECONDS = 5.0
logger = logging.getLogger(__name__)


class CatalogRefreshWorker:
    def __init__(self, database, policy, stop_event):
        self.database = database
        self.policy = policy
        self.stop_event = stop_event
        self.failure = None
        # A kernel-blocked local call cannot be cancelled by Python. Do not let
        # that thread defeat bounded process shutdown; mounted I/O is isolated.
        self.thread = Thread(target=self.run, name="catalog-refresh", daemon=True)

    def start(self):
        self.thread.start()

    def join(self):
        self.thread.join(JOIN_SECONDS)
        if self.thread.is_alive():
            raise RuntimeError("Catalog worker did not stop within the cleanup bound.")

    def consume_one(self):
        """Capture at most one request; never hold a transaction during a scan."""
        if self.stop_event.is_set():
            return False
        with self.database.transaction() as session:
            token = session.execute(
                select(
                    MediaCatalogState.source_id, MediaCatalogState.requested_generation
                )
                .where(
                    MediaCatalogState.requested_generation
                    > MediaCatalogState.handled_request_generation
                )
                .order_by(MediaCatalogState.source_id)
                .limit(1)
            ).first()
        if token is None:
            return False
        source_id, generation = token
        try:
            reconcile_filesystem_source(
                self.database,
                source_id,
                self.policy,
                cancelled=self.stop_event.is_set,
                request_generation=generation,
            )
        except ScanCancelled:
            # Only runtime shutdown requires replay; edits already supersede
            # old authority, and other handled cancellations need a new request.
            if self.stop_event.is_set():
                return True
        except FilesystemSourceError:
            # #30 records expected outcomes; missing/disabled Sources are also
            # handled without touching storage. Never expose raw storage errors.
            logger.warning(
                "Catalog request for Source %s was not fully ready.", source_id
            )
        with self.database.transaction(write=True) as session:
            state = session.get(MediaCatalogState, source_id)
            if state is not None and state.handled_request_generation < generation:
                if state.interrupted:
                    raise RuntimeError("Catalog completion was not persisted.")
                # A deleted Source has no state to recreate. Edits have already
                # advanced handled authority, and newer requests remain pending.
                if generation <= state.requested_generation:
                    state.handled_request_generation = generation
        return True

    def run(self):
        try:
            while not self.stop_event.is_set():
                self.consume_one()
                self.stop_event.wait(POLL_SECONDS)
        except Exception as error:
            # Database/infrastructure failure must not silently kill the thread
            # while the owning host reports healthy operation.
            self.failure = error
            self.stop_event.set()
