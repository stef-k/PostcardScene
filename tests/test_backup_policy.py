"""Persistence and daily time semantics, without scheduled destination execution."""

from dataclasses import FrozenInstanceError, asdict, replace
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from postcardscene.backup.archive import VerifiedBackup
from postcardscene.backup_policy import (
    BackupHistory,
    BackupPolicy,
    evaluate_backup_due,
    get_backup_policy,
    get_backup_status,
    record_backup_attempt,
    record_backup_success,
    replace_backup_policy,
)
from postcardscene.persistence import Database, DatabaseError
from postcardscene.schema import upgrade_database
from postcardscene.settings import set_timezone

NAME = "postcardscene-backup-20260910T030000000000Z-v0.1.0.dev0.tar.gz"
VERIFIED = VerifiedBackup(
    NAME,
    "a" * 64,
    "2026-09-10T03:00:00.000000Z",
    "0.1.0.dev0",
    0x5053434E,
    "0012_backup_policy",
    "regenerable_reconcile_required",
)


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "state.sqlite3")
    upgrade_database(db.path)
    yield db
    db.engine.dispose()


def instant(value):
    return datetime.fromisoformat(value + "+00:00")


def test_policy_defaults_reopen_and_no_destination_io(database, monkeypatch):
    assert get_backup_policy(database) == (BackupPolicy(), BackupHistory())
    destination = "/unavailable/share/../symlink/backups"

    def forbidden(*args, **kwargs):
        pytest.fail("Policy touched the destination filesystem")

    with monkeypatch.context() as patch:
        for method in (
            "stat",
            "lstat",
            "resolve",
            "iterdir",
            "exists",
            "is_dir",
            "open",
        ):
            patch.setattr(Path, method, forbidden)
        replace_backup_policy(database, True, destination, 23, 30)
    other = Database(database.path)
    try:
        assert get_backup_policy(other) == (
            BackupPolicy(True, destination, 23, 30),
            BackupHistory(),
        )
    finally:
        other.engine.dispose()


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": 1},
        {"enabled": "false"},
        {"destination_path": None},
        {"destination_path": ""},
        {"destination_path": "relative"},
        {"destination_path": "/bad\0name"},
        {"destination_path": "/" * 4097},
        {"destination_path": Path("/backup")},
        {"local_hour": True},
        {"local_hour": -1},
        {"local_hour": 24},
        {"local_hour": 3.5},
        {"retention_count": False},
        {"retention_count": 0},
        {"retention_count": 31},
        {"retention_count": "7"},
    ],
)
def test_invalid_replacement_precedes_transaction(database, monkeypatch, change):
    original = get_backup_policy(database)
    values = dict(
        enabled=True, destination_path="/backup", local_hour=3, retention_count=7
    )
    values.update(change)
    with monkeypatch.context() as patch:
        patch.setattr(
            database,
            "transaction",
            lambda **kw: pytest.fail("Validation opened a transaction"),
        )
        with pytest.raises(ValueError):
            replace_backup_policy(database, **values)
    assert get_backup_policy(database) == original


@pytest.mark.parametrize(
    "enabled,hour,when,satisfied,reason",
    [
        (False, 3, "2026-09-10T12:00:00", None, "disabled"),
        (True, 3, "2026-09-10T02:59:59", None, "before_hour"),
        (True, 3, "2026-09-10T03:00:00", None, "due"),
        (True, 3, "2026-09-10T23:59:59", "2026-09-10", "satisfied"),
        (True, 3, "2026-09-09T12:00:00", "2026-09-10", "satisfied"),
        (True, 0, "2026-09-11T00:00:00", "2026-09-10", "due"),
        (True, 3, "2027-01-01T12:00:00", "2026-09-10", "due"),
    ],
)
def test_daily_due_and_clock_movement(enabled, hour, when, satisfied, reason):
    history = (
        BackupHistory()
        if satisfied is None
        else BackupHistory(1, 2, satisfied, NAME, "ready")
    )
    result = evaluate_backup_due(
        instant(when), "UTC", BackupPolicy(enabled, "/backups", hour), history
    )
    assert result.reason == reason
    assert result.due is (reason == "due")
    assert result.current_local_date == when[:10]
    with pytest.raises(FrozenInstanceError):
        result.due = False


def test_dst_gap_fold_and_local_date():
    policy = BackupPolicy(True, "/backups", 3)
    empty = BackupHistory()
    # Athens jumps from 02:59 to 04:00, catching the absent 03:00 hour.
    assert not evaluate_backup_due(
        instant("2026-03-29T00:59:59"), "Europe/Athens", policy, empty
    ).due
    gap = evaluate_backup_due(
        instant("2026-03-29T01:00:00"), "Europe/Athens", policy, empty
    )
    assert gap.due and gap.current_local_date == "2026-03-29"
    first = evaluate_backup_due(
        instant("2026-10-25T00:30:00"), "Europe/Athens", policy, empty
    )
    assert first.due
    success = BackupHistory(1, 2, first.current_local_date, NAME, "ready")
    assert not evaluate_backup_due(
        instant("2026-10-25T01:30:00"), "Europe/Athens", policy, success
    ).due
    assert evaluate_backup_due(
        instant("2026-10-26T01:00:00"), "Europe/Athens", policy, success
    ).due
    midnight = evaluate_backup_due(
        instant("2026-09-09T21:00:00"),
        "Europe/Athens",
        replace(policy, local_hour=0),
        empty,
    )
    assert midnight.current_local_date == "2026-09-10" and midnight.due


@pytest.mark.parametrize(
    "timezone", ["Mars/Missing", "../UTC", "/etc/passwd", "", None, "x" * 256]
)
def test_unavailable_timezone_has_no_fallback(timezone):
    result = evaluate_backup_due(
        instant("2026-09-10T12:00:00"),
        timezone,
        BackupPolicy(True, "/backups"),
        BackupHistory(),
    )
    assert result.reason == "timezone_unavailable"
    assert not result.due and result.current_local_date is None


def test_failure_preserves_success_and_policy_edits_preserve_satisfaction(database):
    record_backup_attempt(database, 100)
    record_backup_success(
        database, VERIFIED, succeeded_at_ns=200, local_date="2026-09-10"
    )
    good = get_backup_policy(database)[1]
    record_backup_attempt(database, 300)
    failed = get_backup_policy(database)[1]
    assert failed == replace(good, last_attempt_ns=300, last_result="failed")
    for policy in (BackupPolicy(), BackupPolicy(True, "/elsewhere", 0, 1)):
        replace_backup_policy(database, **asdict(policy))
        assert get_backup_policy(database)[1] == failed
        assert not get_backup_status(
            database, instant("2026-09-10T12:00:00")
        ).decision.due
    record_backup_success(
        database,
        VERIFIED,
        succeeded_at_ns=400,
        local_date="2026-09-11",
        retention_degraded=True,
    )
    degraded = get_backup_policy(database)[1]
    assert degraded.last_result == "ready_retention_degraded"
    assert degraded.last_success_ns == 400 and degraded.last_archive_filename == NAME
    with pytest.raises(ValueError, match="backwards"):
        record_backup_success(
            database, VERIFIED, succeeded_at_ns=500, local_date="2026-09-09"
        )
    assert get_backup_policy(database)[1] == degraded


def test_status_uses_application_timezone_and_explicit_destination_opt_in(database):
    set_timezone(database, "Europe/Athens")
    replace_backup_policy(database, True, "/private/destination", 3, 7)
    status = get_backup_status(database, instant("2026-09-10T00:00:00"))
    assert status.timezone == "Europe/Athens" and status.decision.due
    assert status.destination_path is None
    assert "/private" not in repr(asdict(status))
    assert (
        get_backup_status(
            database, instant("2026-09-10T00:00:00"), include_destination=True
        ).destination_path
        == "/private/destination"
    )
    for obj, attr in (
        (status, "enabled"),
        (status.history, "last_result"),
        (get_backup_policy(database)[0], "enabled"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(obj, attr, None)
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE application_settings SET timezone='Unavailable/Zone'"
        )
    status = get_backup_status(database, instant("2026-09-10T12:00:00"))
    assert status.timezone is None and status.decision.reason == "timezone_unavailable"


@pytest.mark.parametrize(
    "assignment",
    [
        "enabled=2",
        "local_hour=24",
        "retention_count=0",
        "destination_path='relative'",
        "last_attempt_ns=-1",
        "last_result='exception text'",
        "last_success_local_date='2026-09-10'",
    ],
)
def test_sql_constraints(database, assignment):
    with pytest.raises(IntegrityError):
        with database.engine.begin() as connection:
            connection.exec_driver_sql(f"UPDATE backup_policy SET {assignment}")


@pytest.mark.parametrize(
    "assignment",
    [
        "enabled=2",
        "local_hour=1.5",
        "last_result='raw failure'",
        "last_success_ns=1, last_success_local_date='2026-02-30', last_archive_filename='bad.tar.gz'",
    ],
)
def test_damaged_state_fails_closed(database, assignment):
    with database.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.exec_driver_sql(f"UPDATE backup_policy SET {assignment}")
    with pytest.raises(DatabaseError, match="damaged"):
        get_backup_policy(database)
    with pytest.raises(DatabaseError, match="damaged"):
        replace_backup_policy(database, False, None, 3, 7)


def test_missing_singleton_and_invalid_success(database):
    for changes in (
        {"succeeded_at_ns": -1},
        {"local_date": "2026-02-30"},
        {"verified": replace(VERIFIED, archive_filename="../" + NAME)},
        {"retention_degraded": 1},
    ):
        args = dict(verified=VERIFIED, succeeded_at_ns=2, local_date="2026-09-10")
        args.update(changes)
        with pytest.raises(ValueError):
            record_backup_success(database, **args)
    assert get_backup_policy(database)[1] == BackupHistory()
    with database.engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM backup_policy")
    with pytest.raises(DatabaseError, match="missing"):
        get_backup_policy(database)
