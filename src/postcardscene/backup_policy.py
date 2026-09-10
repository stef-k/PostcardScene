"""Database-only backup policy and advisory history; no recovery authority."""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import CheckConstraint, String, text, update
from sqlalchemy.orm import Mapped, mapped_column

from postcardscene.backup.archive import NAME, VerifiedBackup
from postcardscene.persistence import Base, Database, DatabaseError

BackupResult = Literal["never", "ready", "failed", "ready_retention_degraded"]
RESULTS = ("never", "ready", "failed", "ready_retention_degraded")


class BackupPolicyState(Base):
    __tablename__ = "backup_policy"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("enabled IN (0, 1)", name="enabled_bool"),
        CheckConstraint(
            "destination_path IS NULL OR (length(destination_path) BETWEEN 1 AND 4096 "
            "AND substr(destination_path, 1, 1) = '/' AND instr(destination_path, char(0)) = 0)",
            name="destination_path",
        ),
        CheckConstraint(
            "enabled = 0 OR destination_path IS NOT NULL", name="destination_required"
        ),
        CheckConstraint(
            "typeof(local_hour) = 'integer' AND local_hour BETWEEN 0 AND 23",
            name="local_hour",
        ),
        CheckConstraint(
            "typeof(retention_count) = 'integer' AND retention_count BETWEEN 1 AND 30",
            name="retention_count",
        ),
        CheckConstraint(
            "last_attempt_ns IS NULL OR (typeof(last_attempt_ns) = 'integer' AND last_attempt_ns >= 0)",
            name="last_attempt_ns",
        ),
        CheckConstraint(
            "last_success_ns IS NULL OR (typeof(last_success_ns) = 'integer' AND last_success_ns >= 0)",
            name="last_success_ns",
        ),
        CheckConstraint(
            "last_success_local_date IS NULL OR (length(last_success_local_date) = 10 AND last_success_local_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')",
            name="success_date",
        ),
        CheckConstraint(
            "last_archive_filename IS NULL OR (length(last_archive_filename) BETWEEN 1 AND 255 AND instr(last_archive_filename, '/') = 0 AND instr(last_archive_filename, char(0)) = 0)",
            name="archive_basename",
        ),
        CheckConstraint(
            "(last_success_ns IS NULL AND last_success_local_date IS NULL AND last_archive_filename IS NULL) OR (last_success_ns IS NOT NULL AND last_success_local_date IS NOT NULL AND last_archive_filename IS NOT NULL)",
            name="success_identity",
        ),
        CheckConstraint(
            "last_result IN ('never', 'ready', 'failed', 'ready_retention_degraded')",
            name="last_result",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=False, server_default="0")
    destination_path: Mapped[str | None] = mapped_column(String(4096))
    local_hour: Mapped[int] = mapped_column(default=3, server_default="3")
    retention_count: Mapped[int] = mapped_column(default=7, server_default="7")
    last_attempt_ns: Mapped[int | None]
    last_success_ns: Mapped[int | None]
    last_success_local_date: Mapped[str | None] = mapped_column(String(10))
    last_archive_filename: Mapped[str | None] = mapped_column(String(255))
    last_result: Mapped[str] = mapped_column(
        String(24), default="never", server_default="never"
    )


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("Backup integer is outside its permitted range.")


def _local_date(value):
    if (
        type(value) is not str
        or len(value) != 10
        or date.fromisoformat(value).isoformat() != value
    ):
        raise ValueError("Backup date must be an ISO local date.")


@dataclass(frozen=True)
class BackupPolicy:
    enabled: bool = False
    destination_path: str | None = field(default=None, repr=False)
    local_hour: int = 3
    retention_count: int = 7

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("Backup enabled must be bool.")
        path = self.destination_path
        if path is None:
            if self.enabled:
                raise ValueError("Enabled backup requires a destination.")
        elif (
            type(path) is not str
            or not 1 <= len(path) <= 4096
            or not path.startswith("/")
            or "\0" in path
        ):
            raise ValueError("Backup destination must be a bounded absolute path.")
        _integer(self.local_hour, 0, 23)
        _integer(self.retention_count, 1, 30)


@dataclass(frozen=True)
class BackupHistory:
    last_attempt_ns: int | None = None
    last_success_ns: int | None = None
    last_success_local_date: str | None = None
    last_archive_filename: str | None = None
    last_result: BackupResult = "never"

    def __post_init__(self):
        for timestamp in (self.last_attempt_ns, self.last_success_ns):
            if timestamp is not None:
                _integer(timestamp, 0, 2**63 - 1)
        identity = (
            self.last_success_ns,
            self.last_success_local_date,
            self.last_archive_filename,
        )
        if any(value is not None for value in identity):
            if any(value is None for value in identity):
                raise ValueError("Backup success identity must be complete.")
            _local_date(self.last_success_local_date)
            name = self.last_archive_filename
            if type(name) is not str or len(name) > 255 or not NAME.fullmatch(name):
                raise ValueError("Invalid backup archive basename.")
        if type(self.last_result) is not str or self.last_result not in RESULTS:
            raise ValueError("Invalid backup result.")
        if (
            self.last_result in ("ready", "ready_retention_degraded")
            and self.last_success_ns is None
        ):
            raise ValueError("Ready backup requires success identity.")


@dataclass(frozen=True)
class BackupDue:
    due: bool
    current_local_date: str | None
    reason: Literal[
        "disabled", "before_hour", "satisfied", "due", "timezone_unavailable"
    ]


def evaluate_backup_due(
    now_utc: datetime, timezone: str, policy: BackupPolicy, history: BackupHistory
) -> BackupDue:
    """Owe at most today's date, without reading a clock, database or destination."""
    if not isinstance(now_utc, datetime) or now_utc.utcoffset() != timedelta(0):
        raise ValueError("Supply an aware UTC datetime.")
    try:
        if type(timezone) is not str or not 1 <= len(timezone) <= 255:
            raise ValueError
        local = now_utc.astimezone(ZoneInfo(timezone))
    except (ValueError, ZoneInfoNotFoundError):
        return BackupDue(False, None, "timezone_unavailable")
    today = local.date().isoformat()
    if not policy.enabled:
        return BackupDue(False, today, "disabled")
    if (
        history.last_success_local_date is not None
        and history.last_success_local_date >= today
    ):
        return BackupDue(False, today, "satisfied")
    if local.hour < policy.local_hour:
        return BackupDue(False, today, "before_hour")
    return BackupDue(True, today, "due")


def read_backup_policy(session) -> tuple[BackupPolicy, BackupHistory]:
    """Detach and validate raw SQLite values, avoiding bool coercion."""
    row = session.execute(
        text(
            "SELECT enabled, destination_path, local_hour, retention_count, last_attempt_ns, last_success_ns, last_success_local_date, last_archive_filename, last_result FROM backup_policy WHERE id = 1"
        )
    ).one_or_none()
    if row is None:
        raise DatabaseError("Backup policy row is missing.")
    try:
        if type(row[0]) is not int or row[0] not in (0, 1):
            raise ValueError
        return BackupPolicy(bool(row[0]), *row[1:4]), BackupHistory(*row[4:])
    except (ValueError, TypeError) as error:
        raise DatabaseError("Backup policy/state is damaged.") from error


def get_backup_policy(database: Database) -> tuple[BackupPolicy, BackupHistory]:
    with database.transaction() as session:
        return read_backup_policy(session)


def replace_backup_policy(
    database: Database,
    enabled: bool,
    destination_path: str | None,
    local_hour: int,
    retention_count: int,
) -> None:
    policy = BackupPolicy(enabled, destination_path, local_hour, retention_count)
    with database.transaction(write=True) as session:
        read_backup_policy(session)
        session.execute(
            update(BackupPolicyState)
            .where(BackupPolicyState.id == 1)
            .values(
                enabled=policy.enabled,
                destination_path=policy.destination_path,
                local_hour=policy.local_hour,
                retention_count=policy.retention_count,
            )
        )


def record_backup_attempt(database: Database, attempted_at_ns: int) -> None:
    """Start or fail an attempt conservatively as failed until verified success.

    A crash leaves no invented in-progress/success claim. Previous success survives.
    Call again with the same attempt timestamp to record failure.
    """
    _integer(attempted_at_ns, 0, 2**63 - 1)
    with database.transaction(write=True) as session:
        read_backup_policy(session)
        session.execute(
            update(BackupPolicyState)
            .where(BackupPolicyState.id == 1)
            .values(last_attempt_ns=attempted_at_ns, last_result="failed")
        )


def record_backup_success(
    database: Database,
    verified: VerifiedBackup,
    *,
    succeeded_at_ns: int,
    local_date: str,
    retention_degraded: bool = False,
) -> None:
    """Record an already verified scheduled result; never verify inside a transaction.

    The caller owns execution/serialization and supplies the satisfied due date.
    This bounded history cannot authorize restore or update.
    """
    if type(verified) is not VerifiedBackup or type(retention_degraded) is not bool:
        raise ValueError("Supply verified backup identity and bool retention result.")
    history = BackupHistory(
        None,
        succeeded_at_ns,
        local_date,
        verified.archive_filename,
        "ready_retention_degraded" if retention_degraded else "ready",
    )
    with database.transaction(write=True) as session:
        _, previous = read_backup_policy(session)
        if previous.last_attempt_ns is None:
            raise ValueError("Record the backup attempt before success.")
        if (
            previous.last_success_local_date is not None
            and local_date < previous.last_success_local_date
        ):
            raise ValueError("Cannot move the satisfied backup date backwards.")
        session.execute(
            update(BackupPolicyState)
            .where(BackupPolicyState.id == 1)
            .values(
                last_success_ns=history.last_success_ns,
                last_success_local_date=history.last_success_local_date,
                last_archive_filename=history.last_archive_filename,
                last_result=history.last_result,
            )
        )


@dataclass(frozen=True)
class BackupStatus:
    enabled: bool
    local_hour: int
    retention_count: int
    timezone: str | None
    decision: BackupDue
    history: BackupHistory
    destination_path: str | None = field(default=None, repr=False)


def get_backup_status(
    database: Database, now_utc: datetime, *, include_destination: bool = False
) -> BackupStatus:
    """Advisory status; destination text requires explicit authenticated UI opt-in."""
    if type(include_destination) is not bool:
        raise ValueError("Destination inclusion must be bool.")
    with database.transaction() as session:
        policy, history = read_backup_policy(session)
        timezone = session.execute(
            text("SELECT timezone FROM application_settings WHERE id = 1")
        ).scalar_one_or_none()
    decision = evaluate_backup_due(now_utc, timezone, policy, history)
    return BackupStatus(
        policy.enabled,
        policy.local_hour,
        policy.retention_count,
        None if decision.reason == "timezone_unavailable" else timezone,
        decision,
        history,
        policy.destination_path if include_destination else None,
    )
