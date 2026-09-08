import json
import sqlite3

import pytest
from sqlalchemy import ForeignKey, String, select, text
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from postcardscene.persistence import (
    APPLICATION_ID,
    SCHEMA_REVISION,
    Database,
    DatabaseError,
)
from postcardscene.schema import upgrade_database
from postcardscene.web import create_app


class FixtureBase(DeclarativeBase):
    pass


class Record(FixtureBase):
    __tablename__ = "test_record"
    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column(String, unique=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("test_record.id"))


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "state.sqlite3")
    upgrade_database(database.path)
    # Fixture-only tables exercise the shared ORM/SQLite seam without shipping a domain.
    with database.engine.begin() as connection:
        FixtureBase.metadata.create_all(connection)
    yield database
    database.engine.dispose()


def test_cli_initialization_identity_and_idempotent_upgrade(tmp_path):
    path = tmp_path / "state.sqlite3"
    app = create_app({"TESTING": True, "DATABASE_PATH": path})
    assert not path.exists()
    runner = app.test_cli_runner()
    assert runner.invoke(args=["db", "check"]).exit_code != 0
    assert not path.exists()
    result = runner.invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output
    identity = json.loads(result.output)
    assert identity["application"] == "postcardscene"
    assert identity["schema_revision"] == SCHEMA_REVISION
    assert identity["sqlite_application_id"] == APPLICATION_ID
    assert identity["application_version"]
    before = path.read_bytes()
    assert runner.invoke(args=["db", "check"]).exit_code == 0
    assert runner.invoke(args=["db", "upgrade"]).exit_code == 0
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert set(
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ) == {
            ("alembic_version",),
            ("administrator",),
            ("application_settings",),
            ("operating_window",),
            ("source",),
            ("widget",),
            ("scene",),
            ("scene_placement",),
            ("sequence",),
            ("sequence_membership",),
            ("media_item",),
            ("media_catalog_state",),
        }
    other = create_app({"DATABASE_PATH": tmp_path / "other.sqlite3"})
    assert other.test_cli_runner().invoke(args=["db", "check"]).exit_code != 0
    assert not (tmp_path / "other.sqlite3").exists()


@pytest.mark.parametrize("kind", ["foreign", "unversioned", "future", "corrupt"])
def test_unknown_database_is_never_repaired(tmp_path, kind):
    path = tmp_path / "unexpected.sqlite3"
    if kind == "corrupt":
        path.write_bytes(b"this is not a SQLite database")
    else:
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE sqliteX (id INTEGER)")
            if kind == "foreign":
                connection.execute("PRAGMA application_id=123")
            if kind == "future":
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
                connection.execute("INSERT INTO alembic_version VALUES ('future')")
    before = path.read_bytes()
    app = create_app({"DATABASE_PATH": path})
    assert path.read_bytes() == before
    for operation in ["check", "upgrade"]:
        assert app.test_cli_runner().invoke(args=["db", operation]).exit_code != 0
        assert path.read_bytes() == before
    with pytest.raises((DatabaseError, SQLAlchemyError)):
        with app.extensions["postcardscene.database"].transaction():
            pytest.fail("Unexpected schema was accepted")


def test_transactions_commit_rollback_constraints_and_integrity(database):
    with database.transaction() as session:
        session.add(Record(value="committed"))
    with pytest.raises(RuntimeError, match="abort"):
        with database.transaction() as session:
            session.add(Record(value="rolled back"))
            session.flush()
            raise RuntimeError("abort")
    with pytest.raises(IntegrityError):
        with database.transaction() as session:
            session.add(Record(value="invalid parent", parent_id=999))
    with database.transaction() as session:
        assert session.scalars(select(Record.value)).all() == ["committed"]
        assert session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert session.execute(text("PRAGMA synchronous")).scalar_one() == 2
        assert session.execute(text("PRAGMA busy_timeout")).scalar_one() == 5000
        assert session.execute(text("PRAGMA wal_autocheckpoint")).scalar_one() == 1000
    assert database.check().schema_revision == SCHEMA_REVISION
    # Simulate externally damaged referential integrity; check must not repair it.
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            "INSERT INTO test_record (value, parent_id) VALUES ('broken', 999)"
        )
    with pytest.raises(DatabaseError, match="foreign-key"):
        database.check()
    with pytest.raises(DatabaseError, match="foreign-key"):
        upgrade_database(database.path)


def test_independent_connections_read_during_write_and_fail_bounded_contention(
    database,
):
    player = Database(database.path, timeout=0.05)
    try:
        with database.transaction() as web:
            web.add(Record(value="pending"))
            web.flush()
            with player.transaction() as reader:
                assert reader.scalars(select(Record.value)).all() == []
            # A separate connection cannot acquire the occupied writer lock.
            with player.engine.connect() as writer:
                assert writer.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 50
                with pytest.raises(OperationalError, match="locked"):
                    writer.exec_driver_sql(
                        "INSERT INTO test_record (value) VALUES ('blocked')"
                    )
        with player.transaction() as session:
            assert session.scalars(select(Record.value)).all() == ["pending"]
            session.add(Record(value="recovered"))
        with database.transaction() as session:
            assert session.scalars(select(Record.value).order_by(Record.id)).all() == [
                "pending",
                "recovered",
            ]
    finally:
        player.engine.dispose()
