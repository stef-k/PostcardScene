"""Typed appliance settings, owned by explicit short database transactions."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from postcardscene.persistence import Base, DatabaseError


class ApplicationSettings(Base):
    __tablename__ = "application_settings"
    __table_args__ = (CheckConstraint("id = 1", name="single_settings"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    timezone: Mapped[str] = mapped_column(String(255))


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
