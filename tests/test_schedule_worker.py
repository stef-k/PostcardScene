from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic

import pytest

from postcardscene.operating_schedule import (
    WeeklyWindow,
    get_operating_schedule,
    replace_operating_schedule,
    set_temporary_override,
)
from postcardscene.persistence import Database
from postcardscene.runtime import schedule
from postcardscene.runtime.schedule import OperatingResult, ScheduleWorker
from postcardscene.schema import upgrade_database
from postcardscene.settings import set_timezone


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "state.sqlite3")
    upgrade_database(db.path)
    yield db
    db.engine.dispose()


def run_passes(database, times, *, before=None, target=None):
    stop = Event()
    calls, statuses, waits = [], [], []
    index = 0

    def apply(active, cancelled):
        assert not cancelled()
        calls.append(active)
        return target(active) if target else OperatingResult.APPLIED

    def wait(seconds):
        nonlocal index
        waits.append(seconds)
        statuses.append(worker.status)
        index += 1
        if index == len(times):
            stop.set()
        elif before:
            before(index)

    worker = ScheduleWorker(
        database, apply, stop, clock=lambda: times[index], wait=wait, elapsed=lambda: 0
    )
    worker.start()
    worker.thread.join(3)
    worker.join()
    assert worker.status.state == "stopped"
    assert waits == [15.0] * len(times)
    return calls, statuses


def test_startup_configuration_timezone_and_unchanged_state(database):
    now = datetime(2026, 9, 7, 10, tzinfo=UTC)

    def edit(index):
        if index == 2:
            replace_operating_schedule(
                database, True, (WeeklyWindow(0, 12 * 60, 14 * 60),)
            )
        if index == 3:
            set_timezone(database, "Europe/Athens")

    calls, statuses = run_passes(database, [now] * 5, before=edit)
    assert calls == [True, False, True]
    assert all(status.state == "ready" for status in statuses)
    with pytest.raises(FrozenInstanceError):
        statuses[0].reason = "secret"


@pytest.mark.parametrize(
    "times,expected",
    [
        (
            [
                "2026-09-07T09:59",
                "2026-09-07T10:00",
                "2026-10-12T11:00",
                "2026-09-07T09:00",
                "2027-09-06T10:00",
            ],
            [False, True, False, True],
        ),
        (["2026-03-29T00:59", "2026-03-29T01:00", "2026-03-29T01:30"], [True, False]),
        (["2026-10-25T00:15", "2026-10-25T01:15", "2026-10-25T01:30"], [True, False]),
    ],
)
def test_boundaries_jumps_downtime_and_dst(database, times, expected):
    zone = "UTC" if len(times) == 5 else "Europe/Berlin"
    set_timezone(database, zone)
    windows = (
        (WeeklyWindow(0, 600, 660),) if zone == "UTC" else (WeeklyWindow(6, 60, 150),)
    )
    replace_operating_schedule(database, True, windows)
    calls, _ = run_passes(
        database, [datetime.fromisoformat(t).replace(tzinfo=UTC) for t in times]
    )
    assert calls == expected


def test_read_and_clock_failures_hold_state_and_retry(database, monkeypatch):
    now = datetime(2026, 9, 7, tzinfo=UTC)
    original = schedule.get_operating_schedule

    def fail(_):
        raise RuntimeError("private /path and windows")

    def edit(index):
        monkeypatch.setattr(
            schedule, "get_operating_schedule", fail if index == 1 else original
        )
        replace_operating_schedule(database, True, ())

    calls, statuses = run_passes(
        database, [now, now, now.replace(tzinfo=None), now], before=edit
    )
    assert calls == [True, False]
    assert [s.state for s in statuses] == ["ready", "degraded", "degraded", "ready"]
    assert statuses[1].applied_active is True
    assert statuses[2].desired_active is True
    assert "private" not in repr(statuses)


@pytest.mark.parametrize("raises", [False, True])
def test_target_unavailable_retries(database, raises):
    attempts = 0

    def target(active):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if raises:
                raise RuntimeError("private URL")
            return OperatingResult.UNAVAILABLE
        return OperatingResult.APPLIED

    calls, statuses = run_passes(database, [datetime.now(UTC)] * 3, target=target)
    assert calls == [True, True]
    assert [s.state for s in statuses] == ["degraded", "ready", "ready"]
    assert statuses[0].desired_active is True
    assert statuses[0].applied_active is None
    assert "private" not in repr(statuses)


@pytest.mark.parametrize("concurrent", [False, True])
def test_expiry_cleanup_failure_and_compare_clear(database, monkeypatch, concurrent):
    now = datetime(2026, 9, 7, tzinfo=UTC)
    set_temporary_override(database, False, 1, now=now)
    original = schedule.clear_expired_override
    attempts = 0

    def clear(db, observed, *, now):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("private expiry")
        if concurrent:
            set_temporary_override(db, False, 10, now=now)
        return original(db, observed, now=now)

    monkeypatch.setattr(schedule, "clear_expired_override", clear)
    calls, statuses = run_passes(database, [now + timedelta(minutes=2)] * 2 + [now])
    assert calls == ([True, False] if concurrent else [True])
    assert statuses[0].state == "degraded"
    assert statuses[0].applied_active is True
    assert get_operating_schedule(database).override_active is (
        False if concurrent else None
    )


def test_fatal_authority_loss_wakes_host_and_never_reapplies(database):
    stop = Event()
    calls = []

    def target(active, cancelled):
        calls.append(active)
        return OperatingResult.CLEANUP_FAILED

    worker = ScheduleWorker(database, target, stop)
    worker.start()
    assert stop.wait(2)
    with pytest.raises(RuntimeError, match="cleanup_failed"):
        worker.join()
    assert calls == [True]
    assert worker.status.state == "error"
    assert worker.failure == "cleanup_failed"


def test_shutdown_interrupts_real_wait_and_passes_cancellation(database):
    stop, applied = Event(), Event()
    tokens = []

    def target(active, cancelled):
        tokens.append(cancelled)
        applied.set()
        return OperatingResult.APPLIED

    worker = ScheduleWorker(database, target, stop)
    worker.start()
    assert applied.wait(2)
    started = monotonic()
    worker.join()
    assert monotonic() - started < 5
    assert tokens[0]()
    assert worker.status.state == "stopped"
    assert not worker.thread.is_alive()


def test_precancelled_worker_does_no_work(database):
    stop = Event()
    stop.set()
    worker = ScheduleWorker(database, lambda *_: pytest.fail("target after stop"), stop)
    worker.start()
    worker.join()
    assert stop.is_set()
    assert worker.status.applied_active is None


def test_poll_budget_includes_operation_time(database):
    stop = Event()
    waits = []
    elapsed = iter((100.0, 102.0))

    def wait(seconds):
        waits.append(seconds)
        stop.set()

    worker = ScheduleWorker(
        database,
        lambda *_: OperatingResult.APPLIED,
        stop,
        elapsed=lambda: next(elapsed),
        wait=wait,
    )
    worker.start()
    worker.thread.join(2)
    worker.join()
    assert waits == [13.0]


def test_uncooperative_target_shutdown_is_fatal(database, monkeypatch):
    entered, release, stop = Event(), Event(), Event()

    def target(active, cancelled):
        entered.set()
        release.wait(2)
        return OperatingResult.APPLIED

    worker = ScheduleWorker(database, target, stop)
    assert schedule.JOIN_SECONDS == 5.0
    monkeypatch.setattr(schedule, "JOIN_SECONDS", 0.01)
    worker.start()
    try:
        assert entered.wait(2)
        with pytest.raises(RuntimeError, match="shutdown_timeout"):
            worker.join()
        assert stop.is_set()
        assert worker.status.state == "error"
    finally:
        release.set()
        worker.thread.join(2)
    assert worker.status.applied_active is None
    assert worker.failure == "shutdown_timeout"
