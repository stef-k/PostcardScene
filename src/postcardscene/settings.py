"""Typed appliance settings, owned by explicit short database transactions."""

from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from postcardscene.domain import Sequence
from postcardscene.persistence import Base, Database, DatabaseError


class ApplicationSettings(Base):
    __tablename__ = "application_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_settings"),
        CheckConstraint(
            "display_power_backend IN ('auto', 'cec', 'ddc', 'signal')",
            name="display_power_backend_choice",
        ),
        CheckConstraint(
            "typeof(display_wake_delay_seconds) = 'integer' AND "
            "display_wake_delay_seconds BETWEEN 0 AND 30",
            name="display_wake_delay_range",
        ),
        CheckConstraint(
            "typeof(maximum_static_dwell_seconds) = 'integer' AND "
            "maximum_static_dwell_seconds BETWEEN 300 AND 14400",
            name="maximum_static_dwell_range",
        ),
        CheckConstraint(
            "typeof(schedule_enabled) = 'integer' AND schedule_enabled IN (0, 1)",
            name="schedule_enabled_bool",
        ),
        CheckConstraint(
            "schedule_override_active IS NULL OR (typeof(schedule_override_active) = 'integer' AND schedule_override_active IN (0, 1))",
            name="schedule_override_bool",
        ),
        CheckConstraint(
            "schedule_override_until_utc IS NULL OR (typeof(schedule_override_until_utc) = 'integer' AND schedule_override_until_utc >= 0)",
            name="schedule_override_expiry",
        ),
        CheckConstraint(
            "(schedule_override_active IS NULL) = (schedule_override_until_utc IS NULL)",
            name="schedule_override_pair",
        ),
        CheckConstraint(
            "typeof(default_scene_dwell_seconds) = 'integer' AND "
            "default_scene_dwell_seconds BETWEEN 1 AND 86400",
            name="default_scene_dwell_range",
        ),
    )

    display_power_backend: Mapped[str] = mapped_column(
        String(6), default="auto", server_default="auto"
    )
    display_wake_delay_seconds: Mapped[int] = mapped_column(
        default=5, server_default="5"
    )
    maximum_static_dwell_seconds: Mapped[int] = mapped_column(
        default=1800, server_default="1800"
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_enabled: Mapped[bool] = mapped_column(default=False, server_default="0")
    schedule_override_active: Mapped[bool | None]
    schedule_override_until_utc: Mapped[int | None]
    timezone: Mapped[str] = mapped_column(String(255))
    active_sequence_id: Mapped[int | None] = mapped_column(
        ForeignKey("sequence.id", ondelete="SET NULL")
    )
    default_scene_dwell_seconds: Mapped[int] = mapped_column(
        default=30, server_default="30"
    )


def validate_timezone(value: str) -> None:
    """Accept timezone database keys, never paths or arbitrary offset strings."""
    try:
        if not value or len(value) > 255:
            raise ValueError
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError(
            "Enter an IANA timezone available on this host, such as Europe/Athens or UTC."
        ) from error


def get_timezone(database) -> str:
    with database.transaction() as session:
        settings = session.get(ApplicationSettings, 1)
        if settings is None:
            raise DatabaseError("Application settings row is missing.")
        return settings.timezone


def set_timezone(database, value: str) -> None:
    validate_timezone(value)
    with database.transaction() as session:
        settings = session.get(ApplicationSettings, 1)
        if settings is None:
            raise DatabaseError("Application settings row is missing.")
        settings.timezone = value


@dataclass(frozen=True)
class PlaybackSettings:
    active_sequence_id: int | None
    default_scene_dwell_seconds: int


def read_playback_settings(session) -> PlaybackSettings:
    """Detach settings within a caller-owned short snapshot transaction."""
    row = session.get(ApplicationSettings, 1)
    if row is None:
        raise DatabaseError("Application settings row is missing.")
    if (
        type(row.default_scene_dwell_seconds) is not int
        or not 1 <= row.default_scene_dwell_seconds <= 86400
    ):
        raise DatabaseError("Application playback settings are damaged.")
    return PlaybackSettings(row.active_sequence_id, row.default_scene_dwell_seconds)


def get_playback_settings(database: Database) -> PlaybackSettings:
    with database.transaction() as session:
        return read_playback_settings(session)


def set_active_sequence(database: Database, value: int | None) -> None:
    if value is not None and (type(value) is not int or not 1 <= value <= 2**63 - 1):
        raise ValueError("Sequence identity must be a positive SQLite integer or None.")
    with database.transaction(write=True) as session:
        read_playback_settings(session)
        if value is not None and session.get(Sequence, value) is None:
            raise ValueError("Sequence does not exist.")
        session.get(ApplicationSettings, 1).active_sequence_id = value


def set_default_scene_dwell(database: Database, value: int) -> None:
    if type(value) is not int or not 1 <= value <= 86400:
        raise ValueError("Default dwell must be an integer from 1 to 86400 seconds.")
    with database.transaction(write=True) as session:
        read_playback_settings(session)
        session.get(ApplicationSettings, 1).default_scene_dwell_seconds = value


@dataclass(frozen=True)
class DisplayPowerSettings:
    """Durable panel policy; enforcement belongs to the later runtime owner."""

    display_power_backend: str
    display_wake_delay_seconds: int
    maximum_static_dwell_seconds: int

    def __post_init__(self):
        if type(
            self.display_power_backend
        ) is not str or self.display_power_backend not in (
            "auto",
            "cec",
            "ddc",
            "signal",
        ):
            raise ValueError("Display power backend must be auto, cec, ddc or signal.")
        if (
            type(self.display_wake_delay_seconds) is not int
            or not 0 <= self.display_wake_delay_seconds <= 30
        ):
            raise ValueError("Wake delay must be an integer from 0 to 30 seconds.")
        if (
            type(self.maximum_static_dwell_seconds) is not int
            or not 300 <= self.maximum_static_dwell_seconds <= 14400
        ):
            raise ValueError(
                "Maximum static dwell must be an integer from 300 to 14400 seconds."
            )


def read_display_power_settings(session) -> DisplayPowerSettings:
    """Detach validated policy within a caller-owned short database transaction."""
    row = session.get(ApplicationSettings, 1)
    if row is None:
        raise DatabaseError("Application settings row is missing.")
    try:
        return DisplayPowerSettings(
            row.display_power_backend,
            row.display_wake_delay_seconds,
            row.maximum_static_dwell_seconds,
        )
    except ValueError as error:
        raise DatabaseError(
            "Application display power settings are damaged."
        ) from error


def get_display_power_settings(database: Database) -> DisplayPowerSettings:
    with database.transaction() as session:
        return read_display_power_settings(session)


def set_display_power_settings(
    database: Database,
    display_power_backend: str,
    display_wake_delay_seconds: int,
    maximum_static_dwell_seconds: int,
) -> None:
    """Validate the complete replacement before acquiring the short write transaction."""
    policy = DisplayPowerSettings(
        display_power_backend, display_wake_delay_seconds, maximum_static_dwell_seconds
    )
    with database.transaction(write=True) as session:
        read_display_power_settings(session)
        row = session.get(ApplicationSettings, 1)
        row.display_power_backend = policy.display_power_backend
        row.display_wake_delay_seconds = policy.display_wake_delay_seconds
        row.maximum_static_dwell_seconds = policy.maximum_static_dwell_seconds
