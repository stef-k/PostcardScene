import sqlite3
from dataclasses import FrozenInstanceError

import pytest
from alembic import command
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from postcardscene import domain as d
from postcardscene.persistence import SCHEMA_REVISION, Database, DatabaseError
from postcardscene.playback_configuration import (
    Eligibility as E,
)
from postcardscene.playback_configuration import (
    resolve_active_sequence,
)
from postcardscene.schema import migration_config, upgrade_database
from postcardscene.settings import (
    get_playback_settings,
    set_active_sequence,
    set_default_scene_dwell,
)


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "playback.sqlite3")
    upgrade_database(db.path)
    with db.transaction() as session:
        widget = d.create_widget(
            session, name="Pair", kind="portrait_image_pair", configuration={}
        )
        scene = d.create_scene(
            session, name="Scene", layout="single", placements=[("main", widget.id)]
        )
        d.create_sequence(
            session,
            name="Sequence",
            mode="ordered",
            memberships=[(scene.id, None), (scene.id, 12)],
        )
    yield db
    db.engine.dispose()


def test_settings_and_selection_deletion(database):
    assert get_playback_settings(database).active_sequence_id is None
    assert get_playback_settings(database).default_scene_dwell_seconds == 30
    assert resolve_active_sequence(database).eligibility == E.IDLE
    for enabled in (True, False):
        with database.transaction() as session:
            d.get_sequence(session, 1).enabled = enabled
        set_active_sequence(database, 1)
        assert get_playback_settings(database).active_sequence_id == 1
    set_active_sequence(database, None)
    assert resolve_active_sequence(database).eligibility == E.IDLE
    set_active_sequence(database, 1)
    with database.transaction() as session:
        d.create_sequence(
            session, name="Other", mode="shuffle", memberships=[(1, None)]
        )
        d.remove_sequence(session, 1)
    assert get_playback_settings(database).active_sequence_id is None
    with database.transaction() as session:
        assert len(d.list_sequences(session)) == 1
        assert len(d.list_scenes(session)) == len(d.list_widgets(session)) == 1


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, "1", 999, 2**80])
def test_invalid_selection_preserves_settings(database, value):
    set_active_sequence(database, 1)
    with pytest.raises(ValueError):
        set_active_sequence(database, value)
    assert get_playback_settings(database).active_sequence_id == 1


@pytest.mark.parametrize("value", [True, None, 0, -1, 86401, 1.5, "30"])
def test_invalid_dwell_preserves_settings(database, value):
    with pytest.raises(ValueError):
        set_default_scene_dwell(database, value)
    assert get_playback_settings(database).default_scene_dwell_seconds == 30


def test_dwell_bounds_and_database_constraints(database):
    for value in (1, 86400, 30):
        set_default_scene_dwell(database, value)
        assert get_playback_settings(database).default_scene_dwell_seconds == value
    for assignment in (
        "default_scene_dwell_seconds=0",
        "default_scene_dwell_seconds=1.5",
        "active_sequence_id=999",
    ):
        with pytest.raises(IntegrityError), database.engine.begin() as connection:
            connection.exec_driver_sql(f"UPDATE application_settings SET {assignment}")


def test_detached_coherent_snapshot_and_reread(database):
    set_active_sequence(database, 1)
    connections = []
    event.listen(database.engine, "checkout", lambda *args: connections.append(1))
    event.listen(database.engine, "checkin", lambda *args: connections.pop())
    active = resolve_active_sequence(database)
    assert not connections
    assert active.sequence.mode == "ordered"
    assert [m.scene_id for m in active.sequence.memberships] == [1, 1]
    assert [m.position for m in active.sequence.memberships] == [0, 1]
    member = active.sequence.memberships[1]
    result = resolve_active_sequence(database, membership_id=member.id)
    assert not connections
    assert result.eligibility == E.READY
    assert result.membership.duration_override_seconds == 12
    assert result.scene.widget_id == 1
    assert result.scene.duration_seconds is None
    with pytest.raises(FrozenInstanceError):
        result.scene.widget_id = 3
    with database.transaction() as session:
        d.get_scene(session, 1).duration_seconds = 20
    assert result.scene.duration_seconds is None
    assert (
        resolve_active_sequence(
            database, membership_id=member.id
        ).scene.duration_seconds
        == 20
    )


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("UPDATE sequence SET enabled=0", E.SEQUENCE_DISABLED),
        ("UPDATE sequence SET mode='damaged'", E.SEQUENCE_DAMAGED),
        ("UPDATE sequence_membership SET position=4 WHERE id=1", E.OCCURRENCE_DAMAGED),
        (
            "UPDATE sequence_membership SET duration_override_seconds=-1",
            E.OCCURRENCE_DAMAGED,
        ),
        ("UPDATE scene SET enabled=0", E.SCENE_DISABLED),
        ("UPDATE scene SET duration_seconds=-1", E.OCCURRENCE_DAMAGED),
        ("DELETE FROM scene_placement", E.OCCURRENCE_DAMAGED),
        ("UPDATE scene_placement SET region='wrong'", E.OCCURRENCE_DAMAGED),
        ("UPDATE scene SET layout='unknown'", E.OCCURRENCE_DAMAGED),
        ("UPDATE sequence_membership SET scene_id=999", E.SCENE_MISSING),
        ("UPDATE application_settings SET active_sequence_id=999", E.SEQUENCE_MISSING),
    ],
)
def test_damaged_or_disabled_configuration(database, sql, expected):
    set_active_sequence(database, 1)
    # Bypass foreign keys only to represent externally damaged durable configuration.
    with sqlite3.connect(database.path) as connection:
        connection.execute(sql)
    assert resolve_active_sequence(database, membership_id=1).eligibility == expected


@pytest.mark.parametrize(
    "layout,regions",
    [("split_vertical", ("left", "right")), ("split_horizontal", ("top", "bottom"))],
)
def test_split_remains_valid_but_not_executable(database, layout, regions):
    with database.transaction() as session:
        widget = d.create_widget(session, name="Second", kind="video", configuration={})
        d.update_scene(
            session,
            1,
            name="Split",
            layout=layout,
            enabled=True,
            duration_seconds=None,
            placements=[(regions[0], 1), (regions[1], widget.id)],
        )
        assert d.get_scene(session, 1).layout == layout
        assert len(d.list_scene_placements(session, 1)) == 2
    set_active_sequence(database, 1)
    result = resolve_active_sequence(database, membership_id=1)
    assert result.eligibility == E.UNSUPPORTED_LAYOUT
    assert result.scene.layout == layout
    assert result.scene.widget_id is None


@pytest.mark.parametrize("identity", [True, 0, 999, "1"])
def test_unknown_occurrence_never_selects_another(database, identity):
    set_active_sequence(database, 1)
    assert (
        resolve_active_sequence(database, membership_id=identity).eligibility
        == E.OCCURRENCE_DAMAGED
    )


def test_missing_settings_is_storage_failure(database):
    with database.engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM application_settings")
    with pytest.raises(DatabaseError):
        resolve_active_sequence(database)


@pytest.mark.parametrize(
    "revision",
    ["0008_catalog_requests", "0009_playback_settings", "0010_operating_schedule"],
)
def test_migration_preserves_complete_existing_state(tmp_path, revision):
    db = Database(tmp_path / "old.sqlite3", create=True)
    with db.engine.begin() as connection:
        command.upgrade(migration_config(connection), revision)
        for sql in (
            "INSERT INTO administrator VALUES (1, 'admin', 'hash', 'identity')",
            "UPDATE application_settings SET timezone='Europe/Athens'",
            "INSERT INTO source VALUES (1, 'Photos', 'local_directory', '{}', 1)",
            "INSERT INTO widget VALUES (1, 'Image', 'image', '{}', 1, 1)",
            "INSERT INTO scene VALUES (1, 'Scene', 'single', 25, 1)",
            "INSERT INTO scene_placement VALUES (1, 1, 1, 0, 'main')",
            "INSERT INTO sequence VALUES (1, 'Sequence', 'shuffle', 0)",
            "INSERT INTO sequence_membership VALUES (1, 1, 1, 0, 40)",
            "INSERT INTO media_item VALUES (1, 1, 'photo.jpg', 'image', 20, 30, 1, 10, 20, 'portrait', 'ready', NULL)",
            "INSERT INTO media_catalog_state VALUES (1, 1, 1, 'ready', 20, 20, 2, 1)",
        ):
            connection.exec_driver_sql(sql)
        if revision != "0008_catalog_requests":
            connection.exec_driver_sql(
                "UPDATE application_settings SET active_sequence_id=1, default_scene_dwell_seconds=47"
            )
        if revision == "0010_operating_schedule":
            connection.exec_driver_sql(
                "UPDATE application_settings SET schedule_enabled=1, "
                "schedule_override_active=0, schedule_override_until_utc=2000000000"
            )
            connection.exec_driver_sql(
                "INSERT INTO operating_window VALUES (1, 2, 600, 900)"
            )
        tables = [
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('application_settings', 'alembic_version')"
            )
        ]
        before = {
            table: connection.exec_driver_sql(f'SELECT * FROM "{table}"').all()
            for table in tables
        }
    assert upgrade_database(db.path).schema_revision == SCHEMA_REVISION
    with db.engine.connect() as connection:
        for table in tables:
            assert (
                connection.exec_driver_sql(f'SELECT * FROM "{table}"').all()
                == before[table]
            )
        assert dict(
            connection.exec_driver_sql("SELECT * FROM application_settings")
            .mappings()
            .one()
        ) == {
            "id": 1,
            "timezone": "Europe/Athens",
            "active_sequence_id": 1 if revision != "0008_catalog_requests" else None,
            "default_scene_dwell_seconds": 47
            if revision != "0008_catalog_requests"
            else 30,
            "schedule_enabled": 1 if revision == "0010_operating_schedule" else 0,
            "schedule_override_active": 0
            if revision == "0010_operating_schedule"
            else None,
            "schedule_override_until_utc": 2000000000
            if revision == "0010_operating_schedule"
            else None,
            "display_power_backend": "auto",
            "display_wake_delay_seconds": 5,
            "maximum_static_dwell_seconds": 1800,
        }
        if revision != "0010_operating_schedule":
            assert (
                connection.exec_driver_sql("SELECT * FROM operating_window").all() == []
            )
    db.engine.dispose()
