"""Explicit dashboard composition; no runtime transport or contributor registry."""

from dataclasses import dataclass
from importlib.metadata import version

from sqlalchemy.exc import SQLAlchemyError

from postcardscene.persistence import DatabaseError


@dataclass(frozen=True)
class StatusRow:
    label: str
    state: str
    detail: str


@dataclass(frozen=True)
class DashboardStatus:
    application_version: str
    rows: tuple[StatusRow, ...]


def database_status(database) -> StatusRow:
    try:
        identity = database.check()
    except (DatabaseError, SQLAlchemyError):
        return StatusRow(
            "Database",
            "Needs attention",
            "Database health could not be verified. Run the host database check for diagnosis. "
            "Schema revision is unverified.",
        )
    return StatusRow(
        "Database", "Healthy", f"Schema revision: {identity.schema_revision}."
    )


def dashboard_status(database) -> DashboardStatus:
    # Later owning issues add concrete rows here, containing their own check failures.
    # Runtime status must wait for #17's actual contract, not probe an invented service.
    return DashboardStatus(
        application_version=version("postcardscene"),
        rows=(
            database_status(database),
            StatusRow(
                "Runtime / player",
                "Unavailable",
                "Not yet connected. Playback status is not available yet.",
            ),
        ),
    )
