import pytest
from alembic import command
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from postcardscene import domain as d
from postcardscene.persistence import SCHEMA_REVISION, Database
from postcardscene.schema import migration_config, upgrade_database


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "sequence.sqlite3")
    upgrade_database(database.path)
    with database.transaction() as session:
        source = d.create_source(
            session, name="Photos", kind="local_directory", configuration={}
        )
        widget = d.create_widget(
            session, name="Image", kind="image", configuration={}, source_id=source.id
        )
        for name in ("A", "B"):
            d.create_scene(
                session,
                name=name,
                layout="single",
                placements=[("main", widget.id)],
                duration_seconds=30,
            )
    yield database
    database.engine.dispose()


def test_upgrade_preserves_existing_state(tmp_path):
    database = Database(tmp_path / "old.sqlite3", create=True)
    statements = [
        "INSERT INTO administrator VALUES (1, 'admin', 'hash', 'identity')",
        "UPDATE application_settings SET timezone = 'Europe/Athens'",
        "INSERT INTO source VALUES (1, 'Photos', 'local_directory', '{}', 1)",
        "INSERT INTO widget VALUES (1, 'Image', 'image', '{}', 0, 1)",
        "INSERT INTO scene VALUES (1, 'Scene', 'single', 30, 0)",
        "INSERT INTO scene_placement VALUES (1, 1, 1, 0, 'main')",
    ]
    tables = [
        "administrator",
        "application_settings",
        "source",
        "widget",
        "scene",
        "scene_placement",
    ]
    with database.engine.begin() as connection:
        command.upgrade(migration_config(connection), "0005_scene")
        for statement in statements:
            connection.exec_driver_sql(statement)
        before = {
            table: connection.exec_driver_sql(
                f"SELECT {'id, timezone' if table == 'application_settings' else '*'} FROM {table}"
            ).all()
            for table in tables
        }
    assert upgrade_database(database.path).schema_revision == SCHEMA_REVISION
    with database.transaction() as session:
        for table in tables:
            assert (
                session.connection()
                .exec_driver_sql(
                    f"SELECT {'id, timezone' if table == 'application_settings' else '*'} FROM {table}"
                )
                .all()
                == before[table]
            )
        assert d.list_sequences(session) == []
        sequence = d.create_sequence(
            session, name="Migrated", mode="ordered", memberships=[(1, None)]
        )
        assert d.list_sequence_memberships(session, sequence.id)[0].scene_id == 1
    database.engine.dispose()


@pytest.mark.parametrize("mode", ["ordered", "shuffle"])
def test_round_trip_and_replacement(database, mode):
    with database.transaction() as session:
        sequence_id = d.create_sequence(
            session,
            name=" Sequence ",
            mode=mode,
            memberships=[(1, None), (2, 1), (1, 86400)],
        ).id
        ids = [row.id for row in d.list_sequence_memberships(session, sequence_id)]
        assert len(set(ids)) == 3
        assert all(type(identity) is int for identity in ids)
    with database.transaction() as session:
        sequence = d.get_sequence(session, sequence_id)
        assert (sequence.name, sequence.mode, sequence.enabled) == (
            "Sequence",
            mode,
            True,
        )
        assert [s.id for s in d.list_sequences(session)] == [sequence_id]
        assert [
            (r.id, r.position, r.scene_id, r.duration_override_seconds)
            for r in d.list_sequence_memberships(session, sequence_id)
        ] == list(zip(ids, range(3), [1, 2, 1], [None, 1, 86400]))
        d.update_sequence(
            session,
            sequence_id,
            name="Changed",
            mode="shuffle",
            enabled=False,
            memberships=[(2, 86400), (1, None)],
        )
    with database.transaction() as session:
        sequence = d.get_sequence(session, sequence_id)
        assert (sequence.name, sequence.mode, sequence.enabled) == (
            "Changed",
            "shuffle",
            False,
        )
        assert [
            (r.position, r.scene_id, r.duration_override_seconds)
            for r in d.list_sequence_memberships(session, sequence_id)
        ] == [(0, 2, 86400), (1, 1, None)]
        assert [s.duration_seconds for s in d.list_scenes(session)] == [30, 30]
        d.remove_sequence(session, sequence_id)
    with database.transaction() as session:
        assert d.list_sequences(session) == []
        assert session.scalars(select(d.SequenceMembership)).all() == []
        assert len(d.list_scenes(session)) == 2
        assert len(d.list_widgets(session)) == len(d.list_sources(session)) == 1
        assert len(session.scalars(select(d.ScenePlacement)).all()) == 2
        with pytest.raises(d.DomainError, match="Sequence does not exist"):
            d.get_sequence(session, sequence_id)


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "future"},
        {"mode": []},
        {"name": " "},
        {"name": None},
        {"name": "x" * 129},
        {"enabled": 1},
        {"enabled": None},
        {"memberships": []},
        {"memberships": None},
        {"memberships": "1"},
        {"memberships": [1]},
        {"memberships": [(1,)]},
        {"memberships": [(1, None, 0)]},
        *[
            {"memberships": [(2, None), (value, None)]}
            for value in [999, 0, -1, 1.5, "1", True]
        ],
        *[
            {"memberships": [(2, None), (1, value)]}
            for value in [0, -1, 86401, 1.5, "1", True]
        ],
    ],
)
def test_invalid_replacement_preserves_committed_state(database, changes):
    with database.transaction() as session:
        sequence_id = d.create_sequence(
            session, name="Original", mode="ordered", memberships=[(1, None)]
        ).id
        membership_id = d.list_sequence_memberships(session, sequence_id)[0].id
    with database.transaction() as session:
        fields = dict(
            name="Changed", mode="shuffle", enabled=False, memberships=[(2, 10)]
        )
        fields.update(changes)
        with pytest.raises(d.DomainError):
            d.update_sequence(session, sequence_id, **fields)
        with pytest.raises(d.DomainError):
            d.create_sequence(session, **fields)
    with database.transaction() as session:
        sequence = d.get_sequence(session, sequence_id)
        assert (sequence.name, sequence.mode, sequence.enabled) == (
            "Original",
            "ordered",
            True,
        )
        assert len(d.list_sequences(session)) == 1
        assert [
            (r.id, r.position, r.scene_id, r.duration_override_seconds)
            for r in d.list_sequence_memberships(session, sequence_id)
        ] == [(membership_id, 0, 1, None)]


def test_disabled_and_shared_scene_lifecycle(database):
    with database.transaction() as session:
        first = d.create_sequence(
            session, name="First", mode="ordered", memberships=[(1, None)]
        ).id
        membership_id = d.list_sequence_memberships(session, first)[0].id
        d.update_scene(
            session,
            1,
            name="Disabled",
            layout="single",
            placements=[("main", 1)],
            duration_seconds=30,
            enabled=False,
        )
        second = d.create_sequence(
            session, name="Second", mode="shuffle", memberships=[(1, 1)], enabled=False
        ).id
        d.update_sequence(
            session,
            second,
            name="Second",
            mode="ordered",
            memberships=[(1, None)],
            enabled=False,
        )
    with database.transaction() as session:
        assert d.get_sequence(session, first).enabled is True
        assert d.get_sequence(session, second).enabled is False
        assert d.list_sequence_memberships(session, first)[0].id == membership_id
        assert d.get_scene(session, 1).enabled is False
        with pytest.raises(d.DomainError, match="referenced by a Sequence"):
            d.remove_scene(session, 1)
    with pytest.raises(IntegrityError):
        with database.transaction() as session:
            session.execute(delete(d.Scene).where(d.Scene.id == 1))
    with database.transaction() as session:
        d.remove_sequence(session, first)
        with pytest.raises(d.DomainError):
            d.remove_scene(session, 1)
        d.remove_sequence(session, second)
        d.remove_scene(session, 1)


def test_database_rejects_duplicate_position(database):
    with database.transaction() as session:
        sequence_id = d.create_sequence(
            session, name="Sequence", mode="ordered", memberships=[(1, None)]
        ).id
    with pytest.raises(IntegrityError):
        with database.transaction() as session:
            session.add(
                d.SequenceMembership(sequence_id=sequence_id, scene_id=2, position=0)
            )
