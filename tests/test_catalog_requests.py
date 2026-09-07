"""Durable manual requests and Source authority at the SQLite/catalog seam."""

from threading import Event

import pytest

from postcardscene import catalog_reconciliation as reconciliation
from postcardscene import domain
from postcardscene.catalog import MediaCatalogState, list_media_items
from postcardscene.catalog_requests import request_catalog_reconciliation
from postcardscene.filesystem_source import InvalidSource, ScanCancelled
from postcardscene.runtime.catalog_refresh import CatalogRefreshWorker


def state(db, source_id):
    with db.transaction() as session:
        row = session.get(MediaCatalogState, source_id)
        session.expunge(row)
        return row


def edit(db, source_id, **changes):
    with db.transaction(write=True) as session:
        source = domain.get_source(session, source_id)
        fields = dict(
            name=source.name,
            kind=source.kind,
            configuration=source.configuration,
            enabled=source.enabled,
        )
        fields.update(changes)
        domain.update_source(session, source_id, **fields)


def test_request_is_db_only_and_coalesces(catalog, monkeypatch):
    db, source_id, _, _ = catalog

    def forbidden(*args, **kwargs):
        pytest.fail("request touched storage/reconciliation")

    monkeypatch.setattr(reconciliation, "reconcile_filesystem_source", forbidden)
    monkeypatch.setattr("pathlib.Path.resolve", forbidden)
    monkeypatch.setattr("os.scandir", forbidden)
    monkeypatch.setattr("PIL.Image.open", forbidden)
    assert request_catalog_reconciliation(db, source_id) == 1
    assert request_catalog_reconciliation(db, source_id) == 2
    row = state(db, source_id)
    assert row.refresh_pending
    assert row.handled_request_generation == row.scan_generation == 0
    assert row.last_result == "never_scanned"
    assert row.last_attempt_ns is row.last_success_ns is None


@pytest.mark.parametrize("change", ["missing", "disabled", "web"])
def test_request_requires_enabled_filesystem_source(catalog, change):
    db, source_id, _, _ = catalog
    if change == "disabled":
        edit(db, source_id, enabled=False)
    elif change == "web":
        edit(db, source_id, kind="web_url", configuration={})
    else:
        source_id += 100
    with pytest.raises(InvalidSource):
        request_catalog_reconciliation(db, source_id)


def test_success_and_new_request_during_scan(catalog, monkeypatch):
    db, source_id, root, policy = catalog
    (root / "movie.mp4").touch()
    request_catalog_reconciliation(db, source_id)
    original = reconciliation.enumerate_local_directory

    def enumerate_and_request(*args, **kwargs):
        request_catalog_reconciliation(db, source_id)
        yield from original(*args, **kwargs)

    monkeypatch.setattr(
        reconciliation, "enumerate_local_directory", enumerate_and_request
    )
    worker = CatalogRefreshWorker(db, policy, Event())
    assert worker.consume_one()
    row = state(db, source_id)
    assert (row.requested_generation, row.handled_request_generation) == (2, 1)
    assert row.last_result == "ready"
    assert row.last_success_ns is not None
    monkeypatch.setattr(reconciliation, "enumerate_local_directory", original)
    assert worker.consume_one()
    assert not state(db, source_id).refresh_pending
    assert not worker.consume_one()


def test_shutdown_cancellation_and_restart(catalog, monkeypatch):
    db, source_id, _, policy = catalog
    stop = Event()
    original = reconciliation.enumerate_local_directory

    def cancel(*args, cancelled, **kwargs):
        stop.set()
        assert cancelled()
        raise ScanCancelled("shutdown")

    monkeypatch.setattr(reconciliation, "enumerate_local_directory", cancel)
    request_catalog_reconciliation(db, source_id)
    CatalogRefreshWorker(db, policy, stop).consume_one()
    assert state(db, source_id).refresh_pending
    assert state(db, source_id).last_result == "cancelled"
    monkeypatch.setattr(reconciliation, "enumerate_local_directory", original)
    CatalogRefreshWorker(db, policy, Event()).consume_one()
    assert not state(db, source_id).refresh_pending


@pytest.mark.parametrize("change", ["path", "recursive", "kind", "disable"])
def test_edit_supersedes_active_scan_and_pending_request(catalog, monkeypatch, change):
    db, source_id, root, policy = catalog
    (root / "old.mp4").touch()
    reconciliation.reconcile_filesystem_source(db, source_id, policy)
    before = state(db, source_id)
    request_catalog_reconciliation(db, source_id)
    original = reconciliation.enumerate_local_directory

    def edit_during_scan(*args, **kwargs):
        if change == "disable":
            edit(db, source_id, enabled=False)
        elif change == "kind":
            edit(db, source_id, kind="mounted_directory")
        else:
            config = {"path": str(root), "recursive": True}
            config[change] = str(root / "new") if change == "path" else False
            edit(db, source_id, configuration=config)
        yield from original(*args, **kwargs)

    monkeypatch.setattr(reconciliation, "enumerate_local_directory", edit_during_scan)
    CatalogRefreshWorker(db, policy, Event()).consume_one()
    row = state(db, source_id)
    assert not row.refresh_pending
    assert not row.interrupted
    with db.transaction() as session:
        items = list_media_items(session, source_id)
    if change == "disable":
        assert len(items) == 1
        assert row.last_result == before.last_result
        assert row.last_success_ns == before.last_success_ns
    else:
        assert items == []
        assert row.last_result == "never_scanned"
        assert row.last_success_ns is row.last_attempt_ns is None


def test_migration_preserves_application_and_catalog(tmp_path):
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from postcardscene.persistence import Base, Database
    from postcardscene.schema import migration_config, upgrade_database

    db = Database(tmp_path / "old.sqlite3", create=True)
    with db.engine.begin() as connection:
        command.upgrade(migration_config(connection), "0007_media_catalog")
        for statement in [
            "INSERT INTO administrator VALUES (1, 'admin', 'hash', 'identity')",
            "UPDATE application_settings SET timezone = 'Europe/Athens'",
            "INSERT INTO source VALUES (1, 'Photos', 'local_directory', '{}', 1)",
            "INSERT INTO widget VALUES (1, 'Image', 'image', '{}', 0, 1)",
            "INSERT INTO scene VALUES (1, 'Scene', 'single', 30, 0)",
            "INSERT INTO scene_placement VALUES (1, 1, 1, 0, 'main')",
            "INSERT INTO sequence VALUES (1, 'Sequence', 'ordered', 1)",
            "INSERT INTO sequence_membership VALUES (1, 1, 1, 0, NULL)",
            "INSERT INTO media_item VALUES (1,1,'a.jpg','image',42,123,7,20,10,'landscape','ready',NULL)",
            "INSERT INTO media_catalog_state VALUES (1,7,7,'ready',123,456)",
        ]:
            connection.exec_driver_sql(statement)
        tables = [
            "administrator",
            "application_settings",
            "source",
            "widget",
            "scene",
            "scene_placement",
            "sequence",
            "sequence_membership",
            "media_item",
        ]
        before = {
            table: connection.exec_driver_sql(
                f"SELECT {'id, timezone' if table == 'application_settings' else '*'} FROM {table}"
            ).all()
            for table in tables
        }
    assert upgrade_database(db.path).schema_revision == "0009_playback_settings"
    with db.engine.connect() as connection:
        for table in tables:
            assert (
                connection.exec_driver_sql(
                    f"SELECT {'id, timezone' if table == 'application_settings' else '*'} FROM {table}"
                ).all()
                == before[table]
            )
        assert connection.exec_driver_sql(
            "SELECT * FROM media_catalog_state"
        ).one() == (1, 7, 7, "ready", 123, 456, 0, 0)
        assert (
            compare_metadata(MigrationContext.configure(connection), Base.metadata)
            == []
        )
    db.engine.dispose()


def test_concurrent_requests_preserve_every_increment(catalog):
    from threading import Barrier, Thread

    db, source_id, _, _ = catalog
    barrier = Barrier(3)
    tokens = []
    failures = []

    def request():
        try:
            barrier.wait(timeout=2)
            tokens.append(request_catalog_reconciliation(db, source_id))
        except Exception as error:
            failures.append(error)

    threads = [Thread(target=request) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=2)
    for thread in threads:
        thread.join(6)
        assert not thread.is_alive()
    assert failures == []
    assert sorted(tokens) == [1, 2]
    assert state(db, source_id).requested_generation == 2


def test_rename_keeps_pending_request_and_invalid_update_rolls_back(catalog):
    db, source_id, _, _ = catalog
    request_catalog_reconciliation(db, source_id)
    edit(db, source_id, name="Renamed")
    assert state(db, source_id).refresh_pending
    with pytest.raises(domain.DomainError):
        edit(db, source_id, name="", configuration={"path": "/different"})
    assert state(db, source_id).refresh_pending
    assert state(db, source_id).scan_generation == 0
