"""Retention ownership is independent of current-app recovery compatibility."""

import json
import sqlite3
from datetime import datetime, timezone

import pytest
from alembic import command
from test_backup import built as built
from test_backup import progress, rewrite, sidecar

from postcardscene import backup
from postcardscene.backup import archive, destination, retention
from postcardscene.backup.files import DB_NAME, MANIFEST_NAME, BackupError
from postcardscene.persistence import Database
from postcardscene.schema import migration_config


@pytest.fixture
def older_pair(built, monkeypatch):
    scratch, current, _ = built
    revision = "0011_display_power_settings"
    old_scratch = scratch / "older"
    old_scratch.mkdir()
    for name in ("config.py", "session.key"):
        (old_scratch / name).write_bytes((scratch / name).read_bytes())
    connection = sqlite3.connect(old_scratch / DB_NAME)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.close()
    database = Database(old_scratch / DB_NAME)
    try:
        with database.engine.begin() as connection:
            command.upgrade(migration_config(connection), revision)
    finally:
        database.engine.dispose()
    with monkeypatch.context() as patch:
        patch.setattr(archive, "SCHEMA_REVISION", revision)
        source = archive.build(
            old_scratch,
            datetime(2026, 9, 1, tzinfo=timezone.utc),
            "0.1.0.dev0",
            progress,
        )
    path = current.parent / source.name
    path.write_bytes(source.read_bytes())
    sidecar(path)
    return path


def test_older_schema_counts_for_retention_but_verify_stays_strict(built, older_pair):
    _, current, verified = built
    with pytest.raises(BackupError, match="incompatible_manifest"):
        destination.verify(older_pair, progress)
    with pytest.raises(BackupError):
        backup.verify(older_pair)
    for count in (2, 1):
        assert retention.retain(
            current.parent, count, current.name, verified.archive_sha256, progress
        ) == {"result": "ready"}
        assert older_pair.exists() == (count == 2)
        assert older_pair.with_name(older_pair.name + ".sha256").exists() == (
            count == 2
        )
    assert backup.verify(current) == verified


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_revision", ""),
        ("schema_revision", "x" * 33),
        ("schema_revision", 11),
        ("schema_revision", "../old"),
        ("archive_kind", "foreign"),
        ("sqlite_application_id", 0),
        ("application_version", "9.0.0"),
        ("members", None),
        ("checksum", None),
        ("incomplete", None),
    ],
)
def test_invalid_older_pairs_remain_untouched(built, older_pair, field, value):
    def change(members):
        result = []
        for info, data in members:
            if info.name == MANIFEST_NAME:
                manifest = json.loads(data)
                if field == "members":
                    manifest["members"][DB_NAME]["sha256"] = "0" * 64
                else:
                    manifest[field] = value
                data = json.dumps(manifest).encode()
            result.append((info, data))
        return result

    checksum = older_pair.with_name(older_pair.name + ".sha256")
    if field == "incomplete":
        checksum.unlink()
    elif field == "checksum":
        checksum.write_text("0" * 64 + f"  {older_pair.name}\n")
    else:
        # Recompute the outer checksum: forged inner ownership/member claims must
        # still fail even when the archive/sidecar pair agrees on its final bytes.
        rewrite(older_pair, change)
    before = {path.name: path.read_bytes() for path in older_pair.parent.iterdir()}
    _, current, verified = built
    retention.retain(current.parent, 1, current.name, verified.archive_sha256, progress)
    assert before == {path.name: path.read_bytes() for path in current.parent.iterdir()}
