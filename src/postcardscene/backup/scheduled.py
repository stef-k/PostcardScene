"""One captured daily obligation, serialized through verified creation and retention."""

import time
from datetime import datetime, timezone

from postcardscene.backup_policy import (
    get_backup_status,
    record_backup_attempt,
    record_backup_success,
)
from postcardscene.persistence import Database

from . import _create_locked, _run
from .files import BackupError
from .operation import operation_lock


def scheduled():
    with operation_lock():
        database = Database()
        try:
            return _scheduled_locked(database)
        finally:
            database.engine.dispose()


def _scheduled_locked(database):
    try:
        status = get_backup_status(
            database, datetime.now(timezone.utc), include_destination=True
        )
    except Exception:
        raise BackupError("policy_unavailable") from None
    if status.decision.reason == "timezone_unavailable":
        raise BackupError("timezone_unavailable")
    if not status.decision.due:
        return {"result": status.decision.reason}
    record_backup_attempt(database, time.time_ns())
    verified = _create_locked(status.destination_path)
    succeeded = time.time_ns()
    success = dict(
        succeeded_at_ns=succeeded, local_date=status.decision.current_local_date
    )
    record_backup_success(database, verified, **success)
    try:
        _run(
            "retain",
            status.destination_path,
            arguments=(
                str(status.retention_count),
                verified.archive_filename,
                verified.archive_sha256,
            ),
        )
    except Exception:
        record_backup_success(database, verified, **success, retention_degraded=True)
        return {
            "result": "ready_retention_degraded",
            "archive_filename": verified.archive_filename,
        }
    return {"result": "ready", "archive_filename": verified.archive_filename}
