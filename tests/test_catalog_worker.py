"""Runtime consumption, isolation and cooperative lifecycle contracts."""

from threading import Event, Thread, get_ident

import pytest

from postcardscene import catalog_reconciliation as reconciliation
from postcardscene import domain
from postcardscene.catalog import MediaCatalogState, list_media_items
from postcardscene.catalog_requests import request_catalog_reconciliation
from postcardscene.filesystem_source import (
    EnumerationFailed,
    ScanCancelled,
    SourceUnavailable,
)
from postcardscene.runtime import Lifecycle, RuntimeHost, catalog_refresh


@pytest.mark.parametrize("failure", [SourceUnavailable, EnumerationFailed])
def test_failed_request_is_handled_and_later_source_succeeds(
    catalog, monkeypatch, failure
):
    db, source_id, root, policy = catalog
    (root / "retained.mp4").touch()
    reconciliation.reconcile_filesystem_source(db, source_id, policy)
    with db.transaction() as session:
        later = domain.create_source(
            session,
            name="Later",
            kind="local_directory",
            configuration={"path": str(root), "recursive": True},
        ).id
    for identity in (source_id, later):
        request_catalog_reconciliation(db, identity)
    original = reconciliation.enumerate_local_directory
    calls = 0

    def fail_first(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise failure("storage failed")
        yield from original(*args, **kwargs)

    monkeypatch.setattr(reconciliation, "enumerate_local_directory", fail_first)
    worker = catalog_refresh.CatalogRefreshWorker(db, policy, Event())
    assert worker.consume_one()
    with db.transaction() as session:
        state = session.get(MediaCatalogState, source_id)
        assert not state.refresh_pending
        assert state.last_result == (
            "unavailable" if failure is SourceUnavailable else "error"
        )
        assert len(list_media_items(session, source_id)) == 1
    assert worker.consume_one()
    assert not worker.consume_one()
    assert calls == 2
    with db.transaction() as session:
        assert session.get(MediaCatalogState, later).last_result == "ready"


@pytest.mark.parametrize("change", ["delete", "disable", "authority"])
def test_captured_request_cannot_scan_superseded_source(catalog, monkeypatch, change):
    db, source_id, root, policy = catalog
    request_catalog_reconciliation(db, source_id)
    original = catalog_refresh.reconcile_filesystem_source

    def edit_before_start(*args, **kwargs):
        with db.transaction(write=True) as session:
            if change == "delete":
                domain.remove_source(session, source_id)
            else:
                domain.update_source(
                    session,
                    source_id,
                    name="Edited",
                    kind="local_directory",
                    configuration={
                        "path": str(root),
                        "recursive": change != "authority",
                    },
                    enabled=change != "disable",
                )
        return original(*args, **kwargs)

    monkeypatch.setattr(
        catalog_refresh, "reconcile_filesystem_source", edit_before_start
    )
    monkeypatch.setattr(
        reconciliation,
        "enumerate_local_directory",
        lambda *a, **k: pytest.fail("superseded Source was scanned"),
    )
    worker = catalog_refresh.CatalogRefreshWorker(db, policy, Event())
    assert worker.consume_one()
    assert not worker.consume_one()


def test_thread_uses_reconciliation_off_host_and_shutdown_joins(catalog, monkeypatch):
    db, source_id, _, policy = catalog
    request_catalog_reconciliation(db, source_id)
    host = RuntimeHost(db, policy)
    entered = Event()
    owner = []
    scanner = []

    def blocked_scan(*args, cancelled, **kwargs):
        scanner.append(get_ident())
        entered.set()
        assert host.stop_event.wait(2)
        assert cancelled()
        raise ScanCancelled("shutdown")

    monkeypatch.setattr(reconciliation, "enumerate_local_directory", blocked_scan)

    def run_host():
        owner.append(get_ident())
        host.run()

    thread = Thread(target=run_host)
    thread.start()
    try:
        assert entered.wait(2)
        assert host.status.state == Lifecycle.RUNNING
        assert scanner != owner
    finally:
        host.request_shutdown()
        thread.join(3)
    assert not thread.is_alive()
    assert not host.catalog_worker.thread.is_alive()
    assert host.status.state == Lifecycle.STOPPED
    with db.transaction() as session:
        assert session.get(MediaCatalogState, source_id).refresh_pending


def test_thread_success_persists_handled_result(catalog, monkeypatch):
    db, source_id, root, policy = catalog
    (root / "movie.mp4").touch()
    request_catalog_reconciliation(db, source_id)
    host = RuntimeHost(db, policy)
    original = host.catalog_worker.consume_one

    def consume_and_stop():
        assert original()
        host.request_shutdown()

    monkeypatch.setattr(host.catalog_worker, "consume_one", consume_and_stop)
    host.run()
    with db.transaction() as session:
        state = session.get(MediaCatalogState, source_id)
        assert state.last_result == "ready"
        assert not state.refresh_pending
        assert len(list_media_items(session, source_id)) == 1
    assert not host.catalog_worker.thread.is_alive()


def test_poll_waits_once_between_bounded_queries(catalog, monkeypatch):
    from sqlalchemy import event

    db, _, _, policy = catalog
    stop = Event()
    worker = catalog_refresh.CatalogRefreshWorker(db, policy, stop)
    queries = []
    waits = []

    def record(connection, cursor, statement, parameters, context, executemany):
        if "FROM media_catalog_state" in statement:
            queries.append((statement, parameters))

    def wait(timeout):
        waits.append(timeout)
        if len(waits) == 2:
            stop.set()
        return stop.is_set()

    event.listen(db.engine, "before_cursor_execute", record)
    monkeypatch.setattr(stop, "wait", wait)
    try:
        worker.run()
    finally:
        event.remove(db.engine, "before_cursor_execute", record)
    assert waits == [1.0, 1.0]
    assert len(queries) == 2
    assert all("LIMIT" in sql and parameters[0] == 1 for sql, parameters in queries)


def test_database_failure_reaches_host_status(catalog, monkeypatch):
    db, _, _, policy = catalog
    host = RuntimeHost(db, policy)

    def fail():
        raise RuntimeError("private storage diagnostic")

    monkeypatch.setattr(host.catalog_worker, "consume_one", fail)
    with pytest.raises(RuntimeError, match="Catalog worker failed"):
        host.run()
    assert host.status.state == Lifecycle.ERROR
    assert not host.catalog_worker.thread.is_alive()


def test_edit_then_new_request_survives_old_captured_token(catalog, monkeypatch):
    db, source_id, root, policy = catalog
    request_catalog_reconciliation(db, source_id)
    original = catalog_refresh.reconcile_filesystem_source

    def edit_before_start(*args, **kwargs):
        with db.transaction(write=True) as session:
            domain.update_source(
                session,
                source_id,
                name="Edited",
                kind="local_directory",
                configuration={"path": str(root), "recursive": False},
                enabled=True,
            )
        assert request_catalog_reconciliation(db, source_id) == 2
        return original(*args, **kwargs)

    worker = catalog_refresh.CatalogRefreshWorker(db, policy, Event())
    with monkeypatch.context() as patch:
        patch.setattr(catalog_refresh, "reconcile_filesystem_source", edit_before_start)
        patch.setattr(
            reconciliation,
            "enumerate_local_directory",
            lambda *a, **k: pytest.fail("old token started new authority"),
        )
        assert worker.consume_one()
    with db.transaction() as session:
        state = session.get(MediaCatalogState, source_id)
        assert state.refresh_pending
        assert state.handled_request_generation == 1
    assert worker.consume_one()
    with db.transaction() as session:
        assert not session.get(MediaCatalogState, source_id).refresh_pending


def test_cleanup_timeout_reports_error_with_bounded_join(catalog, monkeypatch):
    db, _, _, policy = catalog
    host = RuntimeHost(db, policy)
    waits = []
    monkeypatch.setattr(host.catalog_worker, "start", lambda: None)
    monkeypatch.setattr(host.catalog_worker.thread, "join", waits.append)
    monkeypatch.setattr(host.catalog_worker.thread, "is_alive", lambda: True)
    monkeypatch.setattr(host.stop_event, "wait", host.request_shutdown)
    with pytest.raises(RuntimeError, match="cleanup bound"):
        host.run()
    assert waits == [5.0]
    assert host.status.state == Lifecycle.ERROR
    assert host.stop_event.is_set()


def test_unpersisted_completion_does_not_consume_request(catalog, monkeypatch):
    db, source_id, _, policy = catalog
    request_catalog_reconciliation(db, source_id)

    def unavailable(*args, **kwargs):
        raise SourceUnavailable("offline")

    def cannot_finish(*args, **kwargs):
        raise RuntimeError("storage write failed")

    monkeypatch.setattr(reconciliation, "enumerate_local_directory", unavailable)
    monkeypatch.setattr(reconciliation, "_finish", cannot_finish)
    worker = catalog_refresh.CatalogRefreshWorker(db, policy, Event())
    with pytest.raises(RuntimeError, match="completion was not persisted"):
        worker.consume_one()
    with db.transaction() as session:
        state = session.get(MediaCatalogState, source_id)
        assert state.refresh_pending
        assert state.interrupted


def test_delete_during_scan_cannot_recreate_catalog(catalog, monkeypatch):
    db, source_id, root, policy = catalog
    (root / "old.mp4").touch()
    request_catalog_reconciliation(db, source_id)
    original = reconciliation.enumerate_local_directory

    def delete_during_scan(*args, **kwargs):
        with db.transaction(write=True) as session:
            domain.remove_source(session, source_id)
        yield from original(*args, **kwargs)

    monkeypatch.setattr(reconciliation, "enumerate_local_directory", delete_during_scan)
    assert catalog_refresh.CatalogRefreshWorker(db, policy, Event()).consume_one()
    with db.transaction() as session:
        assert session.get(MediaCatalogState, source_id) is None
        assert list_media_items(session, source_id) == []
