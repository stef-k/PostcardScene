import pytest
from alembic import command
from sqlalchemy.exc import IntegrityError

from postcardscene import domain as d
from postcardscene.persistence import SCHEMA_REVISION, Database
from postcardscene.schema import migration_config, upgrade_database


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "domain.sqlite3")
    upgrade_database(database.path)
    yield database
    database.engine.dispose()


def test_upgrade_preserves_foundation_state(tmp_path):
    database = Database(tmp_path / "old.sqlite3", create=True)
    with database.engine.begin() as connection:
        command.upgrade(migration_config(connection), "0003_application_settings")
        connection.exec_driver_sql(
            "INSERT INTO administrator VALUES (1, 'admin', 'existing-hash', 'identity')"
        )
        connection.exec_driver_sql(
            "UPDATE application_settings SET timezone = 'Europe/Athens'"
        )
    assert upgrade_database(database.path).schema_revision == SCHEMA_REVISION
    with database.transaction() as session:
        assert d.list_sources(session) == []
        assert d.list_widgets(session) == []
        connection = session.connection()
        assert connection.exec_driver_sql("SELECT * FROM administrator").one() == (
            1,
            "admin",
            "existing-hash",
            "identity",
        )
        assert (
            connection.exec_driver_sql(
                "SELECT timezone FROM application_settings"
            ).scalar_one()
            == "Europe/Athens"
        )
    database.engine.dispose()


@pytest.mark.parametrize(
    "entity,kinds,create,get,update,list_all",
    [
        (
            d.Source,
            d.SOURCE_KINDS,
            d.create_source,
            d.get_source,
            d.update_source,
            d.list_sources,
        ),
        (
            d.Widget,
            d.WIDGET_KINDS,
            d.create_widget,
            d.get_widget,
            d.update_widget,
            d.list_widgets,
        ),
    ],
)
def test_round_trip_and_replacement(
    database, entity, kinds, create, get, update, list_all
):
    configuration = {"nested": [None, True, 3, 1.5, {"label": "Αθήνα"}]}
    with database.transaction() as session:
        records = [
            create(session, name="  Example  ", kind=kind, configuration=configuration)
            for kind in sorted(kinds)
        ]
        identities = [record.id for record in records]
        assert all(type(identity) is int for identity in identities)
        configuration["nested"].append("caller mutation")
    with database.transaction() as session:
        assert [record.id for record in list_all(session)] == identities
        record = get(session, identities[0])
        assert record.name == "Example" and record.enabled is True
        assert record.configuration == {
            "nested": [None, True, 3, 1.5, {"label": "Αθήνα"}]
        }
        extra = {"source_id": None} if entity is d.Widget else {}
        update(
            session,
            record.id,
            name=" Renamed ",
            kind=sorted(kinds)[-1],
            configuration={"replacement": []},
            enabled=False,
            **extra,
        )
    with database.transaction() as session:
        record = get(session, identities[0])
        assert record.name == "Renamed" and record.enabled is False
        assert record.kind == sorted(kinds)[-1]
        assert record.configuration == {"replacement": []}


@pytest.mark.parametrize(
    "changes",
    [
        {"name": " "},
        {"name": "x" * 129},
        {"name": None},
        {"kind": "future_provider"},
        {"kind": []},
        {"configuration": []},
        {"configuration": None},
        {"configuration": {"nested": {1: "invalid"}}},
        {"configuration": {"value": b"bytes"}},
        {"configuration": {"value": object()}},
        {"configuration": {"value": (1, 2)}},
        {"configuration": {"value": float("nan")}},
        {"configuration": {"value": float("inf")}},
        {"configuration": {"value": "é" * 8192}},
        {"enabled": "false"},
    ],
)
@pytest.mark.parametrize(
    "create,get,update,kind,extra",
    [
        (d.create_source, d.get_source, d.update_source, "local_directory", {}),
        (d.create_widget, d.get_widget, d.update_widget, "image", {"source_id": None}),
    ],
)
def test_invalid_input_leaves_state_unchanged(
    database, changes, create, get, update, kind, extra
):
    original = dict(
        name="Original", kind=kind, configuration={"old": True}, enabled=True
    )
    with database.transaction() as session:
        identity = create(session, **original).id
    with database.transaction() as session:
        proposed = dict(
            original, name="Changed", configuration={"new": True}, enabled=False
        )
        proposed.update(changes)
        # Catch inside the transaction: even a subsequent commit cannot persist a partial update.
        with pytest.raises(d.DomainError):
            update(session, identity, **proposed, **extra)
        with pytest.raises(d.DomainError):
            create(session, **proposed)
    with database.transaction() as session:
        record = get(session, identity)
        assert {key: getattr(record, key) for key in original} == original


def test_relationship_disable_reassign_and_safe_delete(database):
    with database.transaction() as session:
        source_id = d.create_source(
            session, name="Library", kind="mounted_directory", configuration={}
        ).id
        widget_id = d.create_widget(
            session, name="Photo", kind="image", configuration={}, source_id=source_id
        ).id
        empty_id = d.create_widget(
            session, name="Unassigned", kind="web_view", configuration={}
        ).id
    with database.transaction() as session:
        assert d.get_widget(session, empty_id).source_id is None
        d.update_source(
            session,
            source_id,
            name="Library",
            kind="mounted_directory",
            configuration={},
            enabled=False,
        )
        assert d.get_widget(session, widget_id).enabled is True
        with pytest.raises(d.DomainError, match="referenced"):
            d.remove_source(session, source_id)
    # The database also rejects deletion if a caller bypasses the domain operation.
    with pytest.raises(IntegrityError):
        with database.transaction() as session:
            session.delete(d.get_source(session, source_id))
    with database.transaction() as session:
        assert d.get_widget(session, widget_id).source_id == source_id
        d.update_widget(
            session,
            widget_id,
            name="Photo",
            kind="image",
            configuration={},
            enabled=False,
            source_id=None,
        )
        assert d.get_source(session, source_id).enabled is False
        d.update_source(
            session,
            source_id,
            name="Library",
            kind="mounted_directory",
            configuration={},
            enabled=True,
        )
        assert d.get_widget(session, widget_id).enabled is False
        d.update_widget(
            session,
            widget_id,
            name="Photo",
            kind="image",
            configuration={},
            enabled=False,
            source_id=source_id,
        )
        d.remove_widget(session, widget_id)
        assert d.get_source(session, source_id).id == source_id
        d.remove_source(session, source_id)
    with database.transaction() as session:
        assert d.list_sources(session) == []
        assert [widget.id for widget in d.list_widgets(session)] == [empty_id]


def test_missing_source_rejects_widget_update_before_mutation(database):
    with database.transaction() as session:
        widget_id = d.create_widget(
            session, name="Original", kind="image", configuration={}
        ).id
    with database.transaction() as session:
        with pytest.raises(d.DomainError, match="Source"):
            d.update_widget(
                session,
                widget_id,
                name="Changed",
                kind="video",
                configuration={"new": True},
                enabled=False,
                source_id=999,
            )
    with database.transaction() as session:
        widget = d.get_widget(session, widget_id)
        assert (
            widget.name,
            widget.kind,
            widget.configuration,
            widget.enabled,
            widget.source_id,
        ) == ("Original", "image", {}, True, None)
