"""Explicit Alembic operations shared by host tooling and the Flask CLI."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory

from postcardscene.persistence import (
    APPLICATION_ID,
    Database,
    DatabaseError,
    check_integrity,
)


def migration_config(connection=None):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    config.attributes["connection"] = connection
    return config


def upgrade_database(path):
    """Initialize an empty database or upgrade a recognized revision to head.

    Operator must stop all database users first and provision the parent directory.
    """
    database = Database(path, create=True)
    try:
        with database.engine.connect() as connection:
            application_id = connection.exec_driver_sql(
                "PRAGMA application_id"
            ).scalar_one()
            objects = connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
            ).all()
            revisions = MigrationContext.configure(connection).get_current_heads()
            known = {
                r.revision
                for r in ScriptDirectory.from_config(
                    migration_config()
                ).walk_revisions()
            }
            empty = application_id == 0 and not objects
            recognized = (
                application_id == APPLICATION_ID
                and len(revisions) == 1
                and revisions[0] in known
            )
            if not (empty or recognized):
                raise DatabaseError("Refusing to migrate an unrecognized database.")
            check_integrity(connection)
        # journal_mode cannot change inside a transaction. Only migration owns this.
        raw = database.engine.raw_connection()
        try:
            cursor = raw.cursor()
            try:
                if cursor.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                    raise DatabaseError("Unable to enable SQLite WAL.")
            finally:
                cursor.close()
        finally:
            raw.close()
        with database.engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
        return database.check()
    finally:
        database.engine.dispose()
