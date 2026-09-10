"""Upgrade the completed #158 schema without disturbing existing appliance state."""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.exc import IntegrityError

from postcardscene.backup_policy import BackupHistory, BackupPolicy, get_backup_policy
from postcardscene.persistence import SCHEMA_REVISION, Base, Database
from postcardscene.schema import migration_config, upgrade_database


def test_upgrade_from_0011_preserves_appliance_state(tmp_path):
    db = Database(tmp_path / "old.sqlite3", create=True)
    statements = [
        "INSERT INTO administrator VALUES (1, 'admin', 'existing-hash', 'identity')",
        "INSERT INTO source VALUES (1, 'Photos', 'local_directory', '{}', 1)",
        "INSERT INTO widget VALUES (1, 'Image', 'image', '{}', 1, 1)",
        "INSERT INTO scene VALUES (1, 'Scene', 'single', 30, 1)",
        "INSERT INTO scene_placement VALUES (1, 1, 1, 0, 'main')",
        "INSERT INTO sequence VALUES (1, 'Sequence', 'ordered', 1)",
        "INSERT INTO sequence_membership VALUES (1, 1, 1, 0, NULL)",
        "INSERT INTO media_item (id, source_id, relative_path, media_type, size_bytes, mtime_ns, seen_generation) VALUES (1, 1, 'photo.jpg', 'image', 120, 500, 1)",
        "INSERT INTO media_catalog_state (source_id, scan_generation, completed_generation, last_result, last_attempt_ns, last_success_ns, requested_generation, handled_request_generation) VALUES (1, 1, 1, 'ready', 200, 300, 2, 1)",
        "INSERT INTO operating_window VALUES (1, 0, 600, 1200)",
        "UPDATE application_settings SET timezone='Europe/Athens', active_sequence_id=1, default_scene_dwell_seconds=45, schedule_enabled=1, schedule_override_active=0, schedule_override_until_utc=2000000000, display_power_backend='cec'",
    ]
    try:
        with db.engine.begin() as connection:
            command.upgrade(migration_config(connection), "0011_display_power_settings")
            for statement in statements:
                connection.exec_driver_sql(statement)
            tables = (
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name != 'alembic_version'"
                )
                .scalars()
                .all()
            )
            before = {
                table: connection.exec_driver_sql(f'SELECT * FROM "{table}"').all()
                for table in tables
            }
        assert (
            upgrade_database(db.path).schema_revision
            == SCHEMA_REVISION
            == "0012_backup_policy"
        )
        assert get_backup_policy(db) == (BackupPolicy(), BackupHistory())
        with db.engine.connect() as connection:
            for table, rows in before.items():
                assert (
                    connection.exec_driver_sql(f'SELECT * FROM "{table}"').all() == rows
                )
            assert connection.exec_driver_sql("SELECT id FROM backup_policy").all() == [
                (1,)
            ]
            assert (
                compare_metadata(MigrationContext.configure(connection), Base.metadata)
                == []
            )
        with pytest.raises(IntegrityError):
            with db.engine.begin() as connection:
                connection.exec_driver_sql("INSERT INTO backup_policy (id) VALUES (2)")
        upgrade_database(db.path)
        assert get_backup_policy(db) == (BackupPolicy(), BackupHistory())
    finally:
        db.engine.dispose()
