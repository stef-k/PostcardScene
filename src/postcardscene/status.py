"""Explicit dashboard composition; no runtime transport or contributor registry."""

from dataclasses import dataclass
from importlib.metadata import version

from sqlalchemy.exc import SQLAlchemyError

from postcardscene.persistence import DatabaseError
from postcardscene.resource_health import GIB, INSTALLED_ROOTS, inspect_resource


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


def storage_status() -> StatusRow:
    snapshots = tuple(inspect_resource(kind, root) for kind, root in INSTALLED_ROOTS)
    severity = {"healthy": 0, "unavailable": 1, "warning": 2, "critical": 3}
    labels = {"durable_state": "Durable state", "cache": "Cache", "runtime": "Runtime"}
    worst = max(snapshots, key=lambda item: severity[item.state]).state
    details = []
    for item in snapshots:
        detail = f"{labels[item.kind]}: {item.state}"
        if item.available:
            detail += f" ({item.free_bytes / GIB:.2f} GiB free)"
        details.append(detail + ".")
    return StatusRow(
        "Owned storage",
        worst.capitalize(),
        " ".join(details) + " Current host snapshot; check host storage when degraded. "
        "No automatic cleanup.",
    )


def dashboard_status(database) -> DashboardStatus:
    # Later owning issues add concrete rows here, containing their own check failures.
    # Runtime status must wait for #17's actual contract, not probe an invented service.
    return DashboardStatus(
        application_version=version("postcardscene"),
        rows=(
            database_status(database),
            storage_status(),
            StatusRow(
                "Runtime / player",
                "Unavailable",
                "Not yet connected. Playback status is not available yet.",
            ),
        ),
    )
