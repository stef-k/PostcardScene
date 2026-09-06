import pytest
from alembic import command
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from postcardscene import domain as d
from postcardscene.persistence import SCHEMA_REVISION, Database
from postcardscene.schema import migration_config, upgrade_database


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "scene.sqlite3")
    upgrade_database(database.path)
    with database.transaction() as session:
        source = d.create_source(
            session, name="Photos", kind="local_directory", configuration={}
        )
        d.create_widget(
            session,
            name="Pair",
            kind="portrait_image_pair",
            configuration={},
            source_id=source.id,
            enabled=False,
        )
        d.create_widget(session, name="Web", kind="web_view", configuration={})
    yield database
    database.engine.dispose()


def test_upgrade_preserves_existing_state(tmp_path):
    database = Database(tmp_path / "old.sqlite3", create=True)
    with database.engine.begin() as connection:
        command.upgrade(migration_config(connection), "0004_source_widget")
        connection.exec_driver_sql(
            "INSERT INTO administrator VALUES (1, 'admin', 'hash', 'identity')"
        )
        connection.exec_driver_sql(
            "UPDATE application_settings SET timezone = 'Europe/Athens'"
        )
        connection.exec_driver_sql(
            "INSERT INTO source VALUES (1, 'Photos', 'local_directory', '{}', 1)"
        )
        connection.exec_driver_sql(
            "INSERT INTO widget VALUES (1, 'Pair', 'portrait_image_pair', '{}', 0, 1)"
        )
    assert upgrade_database(database.path).schema_revision == SCHEMA_REVISION
    with database.transaction() as session:
        assert d.list_scenes(session) == []
        assert d.get_source(session, 1).name == "Photos"
        widget = d.get_widget(session, 1)
        assert (
            widget.kind,
            widget.configuration,
            widget.enabled,
            widget.source_id,
        ) == ("portrait_image_pair", {}, False, 1)
        assert session.connection().exec_driver_sql(
            "SELECT * FROM administrator"
        ).one() == (1, "admin", "hash", "identity")
        assert (
            session.connection()
            .exec_driver_sql("SELECT timezone FROM application_settings")
            .scalar_one()
            == "Europe/Athens"
        )
    database.engine.dispose()


@pytest.mark.parametrize(
    "layout,placements,expected",
    [
        ("single", [("main", 1)], [(0, "main", 1)]),
        (
            "split_vertical",
            [("right", 2), ("left", 1)],
            [(0, "left", 1), (1, "right", 2)],
        ),
        (
            "split_horizontal",
            [("bottom", 1), ("top", 2)],
            [(0, "top", 2), (1, "bottom", 1)],
        ),
    ],
)
def test_scene_round_trip(database, layout, placements, expected):
    with database.transaction() as session:
        scene_id = d.create_scene(
            session, name=" Scene ", layout=layout, placements=placements
        ).id
    with database.transaction() as session:
        scene = d.get_scene(session, scene_id)
        assert (scene.name, scene.layout, scene.duration_seconds, scene.enabled) == (
            "Scene",
            layout,
            None,
            True,
        )
        assert [s.id for s in d.list_scenes(session)] == [scene_id]
        rows = d.list_scene_placements(session, scene_id)
        assert all(type(p.id) is int for p in rows)
        assert [(p.position, p.region, p.widget_id) for p in rows] == expected
        d.update_scene(
            session,
            scene_id,
            name=" Changed ",
            layout="single",
            placements=[("main", 2)],
            duration_seconds=86400,
            enabled=False,
        )
    with database.transaction() as session:
        scene = d.get_scene(session, scene_id)
        assert (scene.name, scene.layout, scene.duration_seconds, scene.enabled) == (
            "Changed",
            "single",
            86400,
            False,
        )
        assert [
            (p.position, p.region, p.widget_id)
            for p in d.list_scene_placements(session, scene_id)
        ] == [(0, "main", 2)]
        assert d.get_widget(session, 1).enabled is False
        assert d.get_widget(session, 2).enabled is True
        d.remove_scene(session, scene_id)
    with database.transaction() as session:
        assert d.list_scenes(session) == []
        assert session.scalars(select(d.ScenePlacement)).all() == []
        assert len(d.list_widgets(session)) == 2
        assert len(d.list_sources(session)) == 1
        with pytest.raises(d.DomainError, match="Scene does not exist"):
            d.get_scene(session, scene_id)


@pytest.mark.parametrize("duration", [None, 1, 86400])
def test_duration(database, duration):
    with database.transaction() as session:
        scene_id = d.create_scene(
            session,
            name="Scene",
            layout="single",
            placements=[("main", 1)],
            duration_seconds=duration,
        ).id
    with database.transaction() as session:
        assert d.get_scene(session, scene_id).duration_seconds == duration


@pytest.mark.parametrize(
    "changes",
    [
        {"layout": "future"},
        {"layout": []},
        {"placements": []},
        {"placements": [("left", 1)]},
        {"placements": [("main", 1), ("extra", 2)]},
        {"layout": "split_vertical", "placements": [("left", 1), ("left", 2)]},
        {"layout": "split_vertical", "placements": [("left", 1), ("unknown", 2)]},
        {"layout": "split_vertical", "placements": [("left", 1), ("right", 1)]},
        {"placements": [("main", 999)]},
        {"placements": [("main", True)]},
        {"name": " "},
        {"name": "x" * 129},
        {"name": None},
        {"enabled": 1},
        {"enabled": None},
        {"duration_seconds": True},
        {"duration_seconds": 0},
        {"duration_seconds": -1},
        {"duration_seconds": 86401},
        {"duration_seconds": 1.5},
        {"duration_seconds": "1"},
    ],
)
def test_invalid_update_preserves_scene_when_error_is_caught(database, changes):
    with database.transaction() as session:
        scene_id = d.create_scene(
            session, name="Original", layout="single", placements=[("main", 1)]
        ).id
        placement_id = d.list_scene_placements(session, scene_id)[0].id
    with database.transaction() as session:
        fields = dict(
            name="Changed",
            layout="single",
            placements=[("main", 2)],
            duration_seconds=10,
            enabled=False,
        )
        fields.update(changes)
        with pytest.raises(d.DomainError):
            d.update_scene(session, scene_id, **fields)
        with pytest.raises(d.DomainError):
            d.create_scene(session, **fields)
    with database.transaction() as session:
        scene = d.get_scene(session, scene_id)
        assert (scene.name, scene.layout, scene.duration_seconds, scene.enabled) == (
            "Original",
            "single",
            None,
            True,
        )
        assert len(d.list_scenes(session)) == 1
        assert [
            (p.id, p.position, p.region, p.widget_id)
            for p in d.list_scene_placements(session, scene_id)
        ] == [(placement_id, 0, "main", 1)]


def test_reused_widget_delete_restriction(database):
    with database.transaction() as session:
        ids = [
            d.create_scene(
                session, name=name, layout="single", placements=[("main", 1)]
            ).id
            for name in ("First", "Second")
        ]
        with pytest.raises(d.DomainError, match="referenced by a Scene"):
            d.remove_widget(session, 1)
        d.remove_scene(session, ids[0])
        with pytest.raises(d.DomainError, match="referenced by a Scene"):
            d.remove_widget(session, 1)
    with pytest.raises(IntegrityError):
        with database.transaction() as session:
            session.execute(delete(d.Widget).where(d.Widget.id == 1))
    with database.transaction() as session:
        d.remove_scene(session, ids[1])
        d.remove_widget(session, 1)
        assert d.get_source(session, 1).name == "Photos"
