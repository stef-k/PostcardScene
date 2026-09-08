from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from postcardscene.operating_schedule import (
    OperatingSchedule,
    WeeklyWindow,
    clear_expired_override,
    clear_temporary_override,
    evaluate_operating_schedule,
    get_operating_schedule,
    replace_operating_schedule,
    set_temporary_override,
)
from postcardscene.persistence import Database, DatabaseError
from postcardscene.schema import upgrade_database
from postcardscene.settings import set_timezone


def instant(value):
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "state.sqlite3")
    upgrade_database(db.path)
    yield db
    db.engine.dispose()


@pytest.mark.parametrize(
    "values",
    [
        (-1, 0, 60),
        (7, 0, 60),
        (True, 0, 60),
        (0, -1, 60),
        (0, 1440, 1440),
        (0, 0, 1441),
        (0, 0, 0),
        (0, 60, 60),
        (0, 120, 60),
        (0, 0.5, 60),
    ],
)
def test_window_validation(values):
    with pytest.raises(ValueError):
        WeeklyWindow(*values)


def test_atomic_replacement_defaults_and_reopen(database):
    assert get_operating_schedule(database) == OperatingSchedule("UTC")
    windows = (WeeklyWindow(0, 0, 1440),) * 64
    replace_operating_schedule(database, True, windows)
    previous = get_operating_schedule(database)
    for enabled, invalid in [
        (1, ()),
        (False, windows + windows[:1]),
        (False, ((0, 0, 60),)),
    ]:
        with pytest.raises(ValueError):
            replace_operating_schedule(database, enabled, invalid)
        assert get_operating_schedule(database) == previous
    other = Database(database.path)
    try:
        assert get_operating_schedule(other) == previous
    finally:
        other.engine.dispose()
    replace_operating_schedule(database, False, ())
    assert get_operating_schedule(database) == OperatingSchedule("UTC")


@pytest.mark.parametrize(
    "enabled,windows,when,active",
    [
        (False, (), "2026-09-07T12:00:00", True),
        (True, (), "2026-09-07T12:00:00", False),
        (True, (WeeklyWindow(0, 600, 660),), "2026-09-07T10:00:00", True),
        (True, (WeeklyWindow(0, 600, 660),), "2026-09-07T10:59:59", True),
        (True, (WeeklyWindow(0, 600, 660),), "2026-09-07T11:00:00", False),
        (True, (WeeklyWindow(0, 0, 1440),), "2026-09-07T23:59:59", True),
        (True, (WeeklyWindow(0, 0, 1440),), "2026-09-08T00:00:00", False),
    ],
)
def test_baselines_and_half_open_minutes(enabled, windows, when, active):
    decision = evaluate_operating_schedule(
        OperatingSchedule("UTC", enabled, windows), instant(when)
    )
    assert decision.active is active
    assert decision.reason == ("schedule" if enabled else "schedule_disabled")
    assert not decision.override_expired


def test_overnight_week_boundary_and_overlaps():
    schedule = OperatingSchedule(
        "UTC",
        True,
        (
            WeeklyWindow(6, 1380, 1440),
            WeeklyWindow(0, 0, 120),
            WeeklyWindow(0, 60, 180),
        ),
    )
    for when, expected in [
        ("2026-09-06T22:59:59", False),
        ("2026-09-06T23:00:00", True),
        ("2026-09-07T00:00:00", True),
        ("2026-09-07T01:30:00", True),
        ("2026-09-07T02:59:59", True),
        ("2026-09-07T03:00:00", False),
    ]:
        assert evaluate_operating_schedule(schedule, instant(when)).active is expected


@pytest.mark.parametrize("active", [True, False])
def test_override_precedence_expiry_restart_and_backward_clock(database, active):
    start = instant("2026-09-07T10:00:00")
    replace_operating_schedule(database, active, ())  # baseline opposite to override
    set_temporary_override(database, active, 1, now=start)
    reopened = Database(database.path)
    try:
        snapshot = get_operating_schedule(reopened)
        assert snapshot.override_until_utc == int(start.timestamp()) + 60
        before = evaluate_operating_schedule(snapshot, start + timedelta(seconds=59))
        assert before.active is active and before.reason == "override"
        expired = evaluate_operating_schedule(snapshot, start + timedelta(minutes=1))
        assert expired.active is not active and expired.override_expired
        assert not clear_expired_override(reopened, snapshot, now=start)
        assert clear_expired_override(reopened, snapshot, now=start + timedelta(days=5))
        assert not evaluate_operating_schedule(
            get_operating_schedule(reopened), start
        ).override_expired
        assert (
            evaluate_operating_schedule(get_operating_schedule(reopened), start).active
            is not active
        )
    finally:
        reopened.engine.dispose()


def test_compare_clear_preserves_concurrent_override(database):
    now = instant("2026-09-07T10:00:00")
    set_temporary_override(database, True, 1, now=now)
    observed = get_operating_schedule(database)
    writer = Database(database.path)
    try:
        set_temporary_override(writer, False, 10080, now=now)
        newer = get_operating_schedule(writer)
        assert not clear_expired_override(
            database, observed, now=now + timedelta(minutes=1)
        )
        assert get_operating_schedule(database) == newer
        replace_operating_schedule(database, True, (WeeklyWindow(1, 0, 1440),))
        assert (
            get_operating_schedule(database).override_until_utc
            == newer.override_until_utc
        )
        clear_temporary_override(database)
        assert get_operating_schedule(database).override_active is None
    finally:
        writer.engine.dispose()


@pytest.mark.parametrize(
    "active,duration",
    [
        (1, 1),
        (None, 1),
        (True, True),
        (False, 0),
        (True, -1),
        (True, 10081),
        (True, 1.5),
    ],
)
def test_invalid_override_preserves_state(database, active, duration):
    set_temporary_override(database, False, 1)
    previous = get_operating_schedule(database)
    with pytest.raises(ValueError):
        set_temporary_override(database, active, duration)
    assert get_operating_schedule(database) == previous


def test_dst_skipped_and_repeated_minutes():
    # Athens skips 03:00..03:59 in March and repeats that hour in October.
    schedule = OperatingSchedule("Europe/Athens", True, (WeeklyWindow(6, 180, 240),))
    for when in ("2026-03-29T00:59:59", "2026-03-29T01:00:00"):
        assert not evaluate_operating_schedule(schedule, instant(when)).active
    for when in ("2026-10-25T00:30:00", "2026-10-25T01:30:00"):
        assert evaluate_operating_schedule(schedule, instant(when)).active
    assert not evaluate_operating_schedule(
        schedule, instant("2026-10-25T02:00:00")
    ).active


def test_timezone_changes_and_large_clock_jumps(database):
    replace_operating_schedule(database, True, (WeeklyWindow(0, 600, 660),))
    now = instant("2026-09-07T10:00:00")
    old = get_operating_schedule(database)
    assert evaluate_operating_schedule(old, now).active
    set_timezone(database, "Europe/Athens")
    fresh = get_operating_schedule(database)
    assert fresh.windows == old.windows
    assert not evaluate_operating_schedule(fresh, now).active
    for delta in (timedelta(weeks=100), timedelta(weeks=-100)):
        assert evaluate_operating_schedule(old, now + delta).active


def test_naive_non_utc_and_invalid_snapshot_rejected(database):
    for now in (
        datetime(2026, 1, 1),
        datetime.fromisoformat("2026-01-01T00:00:00+02:00"),
    ):
        with pytest.raises(ValueError):
            evaluate_operating_schedule(OperatingSchedule("UTC"), now)
        with pytest.raises(ValueError):
            set_temporary_override(database, True, 1, now=now)
    for values in (
        {"override_active": True},
        {"override_until_utc": 100},
        {"override_active": False, "override_until_utc": -1},
        {"timezone": "Mars/Olympus"},
    ):
        with pytest.raises(ValueError):
            replace(OperatingSchedule("UTC"), **values)


@pytest.mark.parametrize(
    "assignment",
    [
        "schedule_enabled=2",
        "schedule_override_active=2, schedule_override_until_utc=10",
        "schedule_override_active=1",
        "schedule_override_active=0, schedule_override_until_utc=-1",
        "timezone='Mars/Olympus'",
    ],
)
def test_malformed_persisted_settings_fail_closed(database, assignment):
    with database.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.exec_driver_sql(f"UPDATE application_settings SET {assignment}")
    with pytest.raises(DatabaseError, match="damaged"):
        get_operating_schedule(database)


@pytest.mark.parametrize(
    "values",
    [
        (-1, 0, 60),
        (7, 0, 60),
        (0, -1, 60),
        (0, 1440, 1440),
        (0, 0, 1441),
        (0, 0, 0),
        (0, 60, 60),
        (0, 120, 60),
        (0, 0.5, 60),
    ],
)
def test_database_rejects_invalid_windows(database, values):
    with pytest.raises(IntegrityError):
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO operating_window (weekday, start_minute, end_minute) VALUES (?, ?, ?)",
                values,
            )


@pytest.mark.parametrize(
    "assignment",
    [
        "schedule_enabled=2",
        "schedule_enabled=0.5",
        "schedule_override_active=2, schedule_override_until_utc=10",
        "schedule_override_active=1",
        "schedule_override_until_utc=10",
        "schedule_override_active=0, schedule_override_until_utc=-1",
        "schedule_override_active=0, schedule_override_until_utc=1.5",
    ],
)
def test_database_rejects_invalid_override_settings(database, assignment):
    with pytest.raises(IntegrityError):
        with database.engine.begin() as connection:
            connection.exec_driver_sql(f"UPDATE application_settings SET {assignment}")


def test_database_window_limit_and_failed_insert_rolls_back_replacement(database):
    replace_operating_schedule(database, True, (WeeklyWindow(0, 0, 1440),) * 64)
    with pytest.raises(IntegrityError):
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO operating_window (weekday, start_minute, end_minute) VALUES (0, 0, 60)"
            )
    before = get_operating_schedule(database)
    # A real SQLite write failure after deletion must roll back the entire edit.
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER reject_window BEFORE INSERT ON operating_window BEGIN SELECT RAISE(ABORT, 'write failure'); END"
        )
    with pytest.raises(IntegrityError):
        replace_operating_schedule(database, False, (WeeklyWindow(1, 0, 60),))
    assert get_operating_schedule(database) == before


def test_missing_settings_and_damaged_window_fail_closed(database):
    with database.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.exec_driver_sql(
            "INSERT INTO operating_window (weekday, start_minute, end_minute) VALUES (7, 0, 60)"
        )
    with pytest.raises(DatabaseError, match="damaged"):
        get_operating_schedule(database)
    with database.engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM application_settings")
    with pytest.raises(DatabaseError, match="missing"):
        get_operating_schedule(database)
