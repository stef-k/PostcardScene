"""Durable weekly active windows and pure current-instant operating policy."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import floor
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import CheckConstraint, delete, select, text, update
from sqlalchemy.orm import Mapped, mapped_column

from postcardscene.persistence import Base, Database, DatabaseError
from postcardscene.settings import ApplicationSettings, validate_timezone

MAX_WINDOWS = 64
MAX_OVERRIDE_MINUTES = 10080


class OperatingWindow(Base):
    __tablename__ = "operating_window"
    __table_args__ = (
        CheckConstraint(
            "typeof(weekday) = 'integer' AND weekday BETWEEN 0 AND 6",
            name="weekday_range",
        ),
        CheckConstraint(
            "typeof(start_minute) = 'integer' AND start_minute BETWEEN 0 AND 1439",
            name="start_range",
        ),
        CheckConstraint(
            "typeof(end_minute) = 'integer' AND end_minute BETWEEN 1 AND 1440",
            name="end_range",
        ),
        CheckConstraint("start_minute < end_minute", name="day_local_interval"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    weekday: Mapped[int]
    start_minute: Mapped[int]
    end_minute: Mapped[int]


@dataclass(frozen=True)
class WeeklyWindow:
    weekday: int
    start_minute: int
    end_minute: int

    def __post_init__(self):
        for value, low, high in (
            (self.weekday, 0, 6),
            (self.start_minute, 0, 1439),
            (self.end_minute, 1, 1440),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Window weekday/minutes must be integers in range.")
        if self.start_minute >= self.end_minute:
            raise ValueError("Windows must start before they end on the same day.")


def _validate_windows(enabled, windows):
    if type(enabled) is not bool:
        raise ValueError("Schedule enabled must be bool.")
    if type(windows) is not tuple or len(windows) > MAX_WINDOWS:
        raise ValueError("Supply a tuple of at most 64 weekly windows.")
    if any(type(window) is not WeeklyWindow for window in windows):
        raise ValueError("Supply WeeklyWindow records.")


def _validate_override(active, until):
    if active is None and until is None:
        return
    if (
        type(active) is not bool
        or type(until) is not int
        or not 0 <= until <= 2**63 - 1
    ):
        raise ValueError(
            "Override requires bool state and nonnegative UTC Unix seconds."
        )


@dataclass(frozen=True)
class OperatingSchedule:
    timezone: str
    enabled: bool = False
    windows: tuple[WeeklyWindow, ...] = ()
    override_active: bool | None = None
    override_until_utc: int | None = None

    def __post_init__(self):
        if type(self.timezone) is not str:
            raise ValueError("Timezone must be an IANA key.")
        validate_timezone(self.timezone)
        _validate_windows(self.enabled, self.windows)
        _validate_override(self.override_active, self.override_until_utc)


@dataclass(frozen=True)
class OperatingDecision:
    active: bool
    reason: Literal["schedule_disabled", "schedule", "override"]
    override_expired: bool = False


def _utc_seconds(now: datetime) -> float:
    if not isinstance(now, datetime) or now.utcoffset() != timedelta(0):
        raise ValueError("Supply an aware UTC datetime.")
    return now.timestamp()


def evaluate_operating_schedule(
    snapshot: OperatingSchedule, now: datetime
) -> OperatingDecision:
    """Evaluate this instant only; repeated wall minutes obey the same rule."""
    seconds = _utc_seconds(now)
    expired = (
        snapshot.override_until_utc is not None
        and seconds >= snapshot.override_until_utc
    )
    if snapshot.override_active is not None and not expired:
        return OperatingDecision(snapshot.override_active, "override")
    if not snapshot.enabled:
        return OperatingDecision(True, "schedule_disabled", expired)
    local = now.astimezone(ZoneInfo(snapshot.timezone))
    minute = local.hour * 60 + local.minute
    active = any(
        window.weekday == local.weekday()
        and window.start_minute <= minute < window.end_minute
        for window in snapshot.windows
    )
    return OperatingDecision(active, "schedule", expired)


def read_operating_schedule(session) -> OperatingSchedule:
    """Detach a bounded snapshot in a caller-owned transaction; reject damaged data."""
    # Raw values preserve malformed SQLite booleans instead of ORM coercion to True.
    row = session.execute(
        text(
            "SELECT timezone, schedule_enabled, schedule_override_active, "
            "schedule_override_until_utc FROM application_settings WHERE id = 1"
        )
    ).one_or_none()
    if row is None:
        raise DatabaseError("Application settings row is missing.")
    try:
        timezone, enabled, active, until = row
        if type(enabled) is not int or enabled not in (0, 1):
            raise ValueError
        if active is not None and (type(active) is not int or active not in (0, 1)):
            raise ValueError
        windows = tuple(
            WeeklyWindow(*values)
            for values in session.execute(
                select(
                    OperatingWindow.weekday,
                    OperatingWindow.start_minute,
                    OperatingWindow.end_minute,
                )
                .order_by(OperatingWindow.id)
                .limit(MAX_WINDOWS + 1)
            )
        )
        return OperatingSchedule(
            timezone,
            bool(enabled),
            windows,
            None if active is None else bool(active),
            until,
        )
    except ValueError as error:
        raise DatabaseError("Operating schedule settings are damaged.") from error


def get_operating_schedule(database: Database) -> OperatingSchedule:
    with database.transaction() as session:
        return read_operating_schedule(session)


def replace_operating_schedule(
    database: Database, enabled: bool, windows: tuple[WeeklyWindow, ...]
) -> None:
    """Validate the complete replacement before deleting any persisted windows."""
    _validate_windows(enabled, windows)
    with database.transaction(write=True) as session:
        read_operating_schedule(session)
        session.execute(delete(OperatingWindow))
        session.add_all(
            OperatingWindow(
                weekday=w.weekday, start_minute=w.start_minute, end_minute=w.end_minute
            )
            for w in windows
        )
        session.execute(
            update(ApplicationSettings)
            .where(ApplicationSettings.id == 1)
            .values(schedule_enabled=enabled)
        )


def set_temporary_override(
    database: Database,
    active: bool,
    duration_minutes: int,
    *,
    now: datetime | None = None,
) -> None:
    if type(active) is not bool:
        raise ValueError("Override state must be bool.")
    if (
        type(duration_minutes) is not int
        or not 1 <= duration_minutes <= MAX_OVERRIDE_MINUTES
    ):
        raise ValueError(
            "Override duration must be an integer from 1 to 10080 minutes."
        )
    until = (
        floor(_utc_seconds(datetime.now(UTC) if now is None else now))
        + duration_minutes * 60
    )
    _validate_override(active, until)
    with database.transaction(write=True) as session:
        read_operating_schedule(session)
        session.execute(
            update(ApplicationSettings)
            .where(ApplicationSettings.id == 1)
            .values(schedule_override_active=active, schedule_override_until_utc=until)
        )


def clear_temporary_override(database: Database) -> None:
    with database.transaction(write=True) as session:
        read_operating_schedule(session)
        session.execute(
            update(ApplicationSettings)
            .where(ApplicationSettings.id == 1)
            .values(schedule_override_active=None, schedule_override_until_utc=None)
        )


def clear_expired_override(
    database: Database, observed: OperatingSchedule, *, now: datetime
) -> bool:
    """Compare-clear only the observed expired pair, preserving a concurrent edit."""
    seconds = _utc_seconds(now)
    if observed.override_until_utc is None or seconds < observed.override_until_utc:
        return False
    with database.transaction(write=True) as session:
        read_operating_schedule(session)
        result = session.execute(
            update(ApplicationSettings)
            .where(
                ApplicationSettings.id == 1,
                ApplicationSettings.schedule_override_active
                == observed.override_active,
                ApplicationSettings.schedule_override_until_utc
                == observed.override_until_utc,
            )
            .values(schedule_override_active=None, schedule_override_until_utc=None)
        )
        return result.rowcount == 1
