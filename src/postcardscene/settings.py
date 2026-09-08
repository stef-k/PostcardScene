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
