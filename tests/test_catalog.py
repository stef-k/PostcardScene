"""Catalog contracts at real SQLite and filesystem seams."""

from contextlib import contextmanager

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from PIL import Image
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from postcardscene import catalog as c
from postcardscene import catalog_reconciliation as r
from postcardscene import domain as d
from postcardscene.filesystem_source import (
    EnumerationFailed,
    InvalidSource,
    MediaEntry,
    ScanCancelled,
    SourceUnavailable,
)
from postcardscene.persistence import Base, Database
from postcardscene.schema import migration_config, upgrade_database


def scan(catalog, **kwargs):
    db, source_id, _, policy = catalog
    r.reconcile_filesystem_source(db, source_id, policy, **kwargs)


def snapshot(catalog):
    db, source_id, _, _ = catalog
    with db.transaction() as session:
        return {
            row.relative_path: (
                row.id,
                row.metadata_status,
                row.display_width,
                row.display_height,
                row.orientation,
                row.seen_generation,
            )
            for row in c.list_media_items(session, source_id)
        }


def state(catalog):
    db, source_id, _, _ = catalog
    with db.transaction() as session:
        row = session.get(c.MediaCatalogState, source_id)
        return (
            row.scan_generation,
            row.completed_generation,
            row.last_result,
            row.last_attempt_ns,
            row.last_success_ns,
        )


def test_migration_preserves_full_configuration(tmp_path):
    db = Database(tmp_path / "old.sqlite3", create=True)
    with db.engine.begin() as connection:
        command.upgrade(migration_config(connection), "0006_sequence")
        for statement in [
            "INSERT INTO administrator VALUES (1, 'admin', 'hash', 'identity')",
            "UPDATE application_settings SET timezone = 'Europe/Athens'",
            "INSERT INTO source VALUES (1, 'Photos', 'local_directory', '{}', 1)",
            "INSERT INTO widget VALUES (1, 'Image', 'image', '{}', 0, 1)",
            "INSERT INTO scene VALUES (1, 'Scene', 'single', 30, 0)",
            "INSERT INTO scene_placement VALUES (1, 1, 1, 0, 'main')",
            "INSERT INTO sequence VALUES (1, 'Sequence', 'ordered', 1)",
            "INSERT INTO sequence_membership VALUES (1, 1, 1, 0, NULL)",
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
        assert (
            compare_metadata(MigrationContext.configure(connection), Base.metadata)
            == []
        )
    db.engine.dispose()


def test_initial_repeat_change_add_remove_and_identity(catalog, monkeypatch):
    _, _, root, _ = catalog
    Image.new("RGB", (20, 10)).save(root / "photo.jpg")
    (root / "movie.mp4").write_bytes(b"video")
    scan(catalog, batch_size=1)
    first = snapshot(catalog)
    assert first["photo.jpg"][1:5] == ("ready", 20, 10, "landscape")
    assert first["movie.mp4"][1:5] == (None, None, None, None)
    inspect = r.inspect_image
    with monkeypatch.context() as m:
        m.setattr(
            r, "inspect_image", lambda *args: pytest.fail("unchanged image reopened")
        )
        scan(catalog)
    assert snapshot(catalog)["photo.jpg"][:5] == first["photo.jpg"][:5]
    Image.new("RGB", (10, 30)).save(root / "photo.jpg")
    (root / "movie.mp4").unlink()
    (root / "new.mp4").touch()

    def check_invalidated(*args):
        assert snapshot(catalog)["photo.jpg"][1:5] == ("pending", None, None, None)
        return inspect(*args)

    monkeypatch.setattr(r, "inspect_image", check_invalidated)
    scan(catalog, batch_size=1)
    current = snapshot(catalog)
    assert set(current) == {"photo.jpg", "new.mp4"}
    assert current["photo.jpg"][:5] == (
        first["photo.jpg"][0],
        "ready",
        10,
        30,
        "portrait",
    )
    assert state(catalog)[:3] == (3, 3, "ready")
    assert state(catalog)[4] >= state(catalog)[3]


@pytest.mark.parametrize(
    ("failure", "result"),
    [
        (EnumerationFailed, "error"),
        (SourceUnavailable, "unavailable"),
        (ScanCancelled, "cancelled"),
        (InvalidSource, "error"),
        (RuntimeError, "error"),
    ],
)
def test_partial_failure_preserves_unseen(catalog, monkeypatch, failure, result):
    _, _, root, _ = catalog
    (root / "old.mp4").touch()
    scan(catalog)
    success = state(catalog)[4]

    def partial(*args, **kwargs):
        yield MediaEntry("new.mp4", "video", 2, 3)
        raise failure("controlled failure")

    monkeypatch.setattr(r, "enumerate_local_directory", partial)
    with pytest.raises(EnumerationFailed if failure is RuntimeError else failure):
        scan(catalog, batch_size=1)
    assert set(snapshot(catalog)) == {"old.mp4", "new.mp4"}
    assert state(catalog)[:3] == (2, 2, result)
    assert state(catalog)[4] == success


def test_interrupted_then_recovered_and_superseded(catalog, monkeypatch):
    db, source_id, root, _ = catalog
    (root / "old.mp4").touch()
    scan(catalog)
    r._start(db, source_id)  # Process died before recording a handled result.
    assert state(catalog)[:2] == (2, 1)
    scan(catalog)
    assert state(catalog)[:3] == (3, 3, "ready")

    def superseded(*args, **kwargs):
        yield MediaEntry("partial.mp4", "video", 0, 0)
        r._start(db, source_id)

    monkeypatch.setattr(r, "enumerate_local_directory", superseded)
    with pytest.raises(r.ScanSuperseded):
        scan(catalog, batch_size=1)
    assert set(snapshot(catalog)) == {"old.mp4", "partial.mp4"}
    assert state(catalog)[:3] == (5, 3, "ready")


def test_disabled_preserves_and_source_delete_cascades(catalog):
    db, source_id, root, _ = catalog
    (root / "video.mp4").touch()
    scan(catalog)
    before = state(catalog)
    with db.transaction() as session:
        session.get(d.Source, source_id).enabled = False
        other_id = d.create_source(
            session, name="Other", kind="local_directory", configuration={}
        ).id
        session.add(c.MediaCatalogState(source_id=other_id))
    with pytest.raises(InvalidSource):
        scan(catalog)
    assert state(catalog) == before
    assert len(snapshot(catalog)) == 1
    with pytest.raises(IntegrityError):
        with db.transaction() as session:
            row = c.get_media_item(session, source_id, "video.mp4")
            session.add(
                c.MediaItem(
                    source_id=source_id,
                    relative_path=row.relative_path,
                    media_type="video",
                    size_bytes=0,
                    mtime_ns=0,
                    seen_generation=1,
                )
            )
    with db.transaction() as session:
        session.execute(delete(d.Source).where(d.Source.id == source_id))
    assert snapshot(catalog) == {}
    with db.transaction() as session:
        assert session.get(c.MediaCatalogState, source_id) is None
        assert session.get(c.MediaCatalogState, other_id) is not None


def test_batches_release_database_before_filesystem_and_metadata(catalog, monkeypatch):
    db, source_id, root, _ = catalog
    for i in range(7):
        Image.new("RGB", (8, 8)).save(root / f"{i}.png")
    transaction = db.transaction
    active = False

    @contextmanager
    def checked_transaction(**kwargs):
        nonlocal active
        assert not active
        active = True
        try:
            with transaction(**kwargs) as session:
                yield session
        finally:
            active = False

    original = r.enumerate_local_directory
    inspect = r.inspect_image
    observations = []
    observe = r._observe

    def enumeration(*args, **kwargs):
        for entry in original(*args, **kwargs):
            assert not active
            # A control-plane writer can complete between streaming entries.
            with transaction(write=True) as session:
                session.get(d.Source, source_id).name = "Still responsive"
            yield entry

    def metadata(*args):
        assert not active
        return inspect(*args)

    def batch(*args):
        observations.append(len(args[-1]))
        return observe(*args)

    monkeypatch.setattr(db, "transaction", checked_transaction)
    monkeypatch.setattr(r, "enumerate_local_directory", enumeration)
    monkeypatch.setattr(r, "inspect_image", metadata)
    monkeypatch.setattr(r, "_observe", batch)
    scan(catalog, batch_size=2, metadata_batch_size=2)
    assert observations == [2, 2, 2, 1]
    assert len(snapshot(catalog)) == 7
    assert all(row[1] == "ready" for row in snapshot(catalog).values())


def test_queries_and_health_are_bounded(catalog):
    db, source_id, root, _ = catalog
    for i, size in enumerate([(10, 20), (20, 10), (10, 10)]):
        Image.new("RGB", size).save(root / f"{i}.jpg")
    (root / "video.mp4").touch()
    scan(catalog)
    with db.transaction() as session:
        page = c.list_media_items(session, source_id, limit=2)
        next_page = c.list_media_items(
            session, source_id, after_id=page[-1].id, limit=2
        )
        assert len(page) == len(next_page) == 2
        assert page[-1].id < next_page[0].id
        assert c.count_media_items(session, source_id) == 4
        assert c.count_media_items(session, source_id, media_type="video") == 1
        assert (
            c.list_media_items(session, source_id, orientation="portrait")[
                0
            ].relative_path
            == "0.jpg"
        )
        assert c.catalog_health_counts(session, source_id) == {
            ("image", "ready"): 3,
            ("video", None): 1,
        }
        assert c.get_media_item(session, source_id, "0.jpg").identity == (
            source_id,
            "0.jpg",
        )
        assert c.list_media_items(session, source_id + 1) == []
        for limit in (0, 501, None, True):
            with pytest.raises(ValueError):
                c.list_media_items(session, source_id, limit=limit)


def test_cancellation_between_batches_preserves_unseen(catalog, monkeypatch):
    _, _, root, _ = catalog
    (root / "old.mp4").touch()
    scan(catalog)
    cancelled = False

    def entries(*args, **kwargs):
        nonlocal cancelled
        yield MediaEntry("new.mp4", "video", 0, 0)
        cancelled = True
        yield MediaEntry("later.mp4", "video", 0, 0)

    monkeypatch.setattr(r, "enumerate_local_directory", entries)
    with pytest.raises(ScanCancelled):
        scan(catalog, batch_size=1, cancelled=lambda: cancelled)
    assert set(snapshot(catalog)) == {"old.mp4", "new.mp4"}
    assert state(catalog)[:3] == (2, 2, "cancelled")


def test_cleanup_rechecks_generation_each_batch(catalog, monkeypatch):
    db, source_id, root, _ = catalog
    for i in range(5):
        (root / f"{i}.mp4").touch()
    scan(catalog)
    for path in root.iterdir():
        path.unlink()
    transaction = db.transaction
    supersede = False
    removed = []

    @contextmanager
    def interleaved(**kwargs):
        nonlocal supersede
        with transaction(**kwargs) as session:
            before = c.count_media_items(session, source_id)
            yield session
            after = c.count_media_items(session, source_id)
        if before > after:
            removed.append(before - after)
            if not supersede:
                supersede = True
                with transaction(**kwargs) as session:
                    session.get(c.MediaCatalogState, source_id).scan_generation += 1

    monkeypatch.setattr(db, "transaction", interleaved)
    with pytest.raises(r.ScanSuperseded):
        scan(catalog, batch_size=2)
    assert removed == [2]
    assert len(snapshot(catalog)) == 3
    assert state(catalog)[:2] == (3, 1)
