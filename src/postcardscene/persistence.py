"""Shared SQLite ownership; no Flask context or implicit schema creation."""

from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

from alembic.migration import MigrationContext
from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

APPLICATION_ID = 0x5053434E  # PSCN, SQLite file identity (not a release number).
SCHEMA_REVISION = "0003_application_settings"
DEFAULT_DATABASE_PATH = Path("/var/lib/postcardscene/postcardscene.sqlite3")


class Base(DeclarativeBase):
    """All future application models share this framework-independent metadata."""

    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class DatabaseError(RuntimeError):
    """An incompatible database or failed integrity check; never repaired silently."""


@dataclass(frozen=True)
class DatabaseIdentity:
    application: str
    application_version: str
    sqlite_application_id: int
    schema_revision: str


def require_schema(connection):
    """Fail closed before application reads/writes on an unknown schema."""
    application_id = connection.exec_driver_sql("PRAGMA application_id").scalar_one()
    revisions = MigrationContext.configure(connection).get_current_heads()
    if application_id != APPLICATION_ID or revisions != (SCHEMA_REVISION,):
        raise DatabaseError(
            "Database schema is incompatible; run the explicit migration workflow."
        )
    if connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() != "wal":
        raise DatabaseError(
            "Database requires WAL; run the explicit migration workflow."
        )


def check_integrity(connection):
    """Read-only physical and referential checks; also used before migration."""
    if connection.exec_driver_sql("PRAGMA quick_check").scalars().all() != ["ok"]:
        raise DatabaseError("SQLite integrity check failed.")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise DatabaseError("SQLite foreign-key integrity check failed.")


class Database:
    """One engine per process; one connection/session per unit of work."""

    def __init__(self, path=DEFAULT_DATABASE_PATH, *, timeout=5.0, create=False):
        path = Path(path)
        if not path.is_absolute():
            raise ValueError("Database path must be absolute.")
        if not 0 < timeout <= 5:
            raise ValueError(
                "Database timeout must be greater than zero and at most five seconds."
            )
        self.path = path
        # URI mode=rw prevents missing production files becoming empty databases.
        url = URL.create(
            "sqlite+pysqlite",
            database=path.as_uri(),
            query={"mode": "rwc" if create else "rw", "uri": "true"},
        )
        self.engine = create_engine(
            url,
            poolclass=NullPool,
            connect_args={"timeout": timeout},
            hide_parameters=True,
        )
        event.listen(self.engine, "connect", self._configure_connection)
        event.listen(self.engine, "begin", self._begin)
        self._sessions = sessionmaker(self.engine, autoflush=False)

    @staticmethod
    def _configure_connection(connection, _record):
        # Explicit BEGIN also makes DDL and read snapshots transactional on Python 3.11.
        connection.isolation_level = None
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA wal_autocheckpoint=1000")
        finally:
            cursor.close()

    @staticmethod
    def _begin(connection):
        connection.exec_driver_sql("BEGIN")

    @contextmanager
    def transaction(self):
        """Commit success, roll back failure, always close; no implicit retries."""
        with self._sessions.begin() as session:
            require_schema(session.connection())
            yield session

    def check(self):
        """Check identity and SQLite integrity without repairing or migrating."""
        with self.engine.connect() as connection:
            require_schema(connection)
            check_integrity(connection)
        return DatabaseIdentity(
            "postcardscene", version("postcardscene"), APPLICATION_ID, SCHEMA_REVISION
        )
