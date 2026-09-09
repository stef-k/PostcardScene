"""Read-only service/software capabilities; no hardware mutations or impersonation."""

import os
import stat
from pathlib import Path
from threading import Event

from postcardscene.graphics import WaylandSession
from postcardscene.graphics.output import DisplayStatus, select_connector
from postcardscene.graphics.output_probe import (
    ProbeError,
    read_connectors,
    read_outputs,
)
from postcardscene.power._command import TOOLS
from postcardscene.power._types import Kind
from postcardscene.resource_health import INSTALLED_ROOTS, inspect_resource

from . import Check
from .bounded import systemd_state
from .metadata import identities


def service(identifier):
    name = identifier.removeprefix("service_")
    fields = systemd_state(f"postcardscene-{name}.service")
    reason = "ready"
    if fields["LoadState"] != "loaded":
        reason = "not_installed"
    elif fields["ActiveState"] == "failed":
        reason = "service_failed"
    elif fields["ActiveState"] != "active":
        reason = "not_running"
    elif fields["UnitFileState"] != "enabled":
        reason = "not_enabled"
    return Check(
        identifier,
        "ready" if reason == "ready" else "degraded",
        reason,
        tuple(
            (key, fields[key]) for key in ("LoadState", "UnitFileState", "ActiveState")
        ),
    )


def storage(identifier):
    kind = identifier.removeprefix("storage_")
    root = dict(INSTALLED_ROOTS)[kind]
    result = inspect_resource(kind, root)
    state = {
        "healthy": "ready",
        "warning": "degraded",
        "critical": "degraded",
        "unavailable": "unavailable",
    }[result.state]
    return Check(identifier, state, result.reason, (("space_state", result.state),))


def graphics(identifier):
    runtime, _, _, _ = identities()
    if os.geteuid() != runtime:
        return Check(identifier, "unavailable", "capability_unavailable")
    session = WaylandSession()
    status = DisplayStatus(False, "degraded", "session_unavailable")
    if session.inspect().available:
        try:
            connector = select_connector(read_connectors(), None)
            if connector is None:
                status = DisplayStatus(True, "no_display", "no_display")
            else:
                outputs = read_outputs(session.client_environment(), Event())
                ready = any(
                    output.name == connector.name
                    and output.enabled
                    and output.current is not None
                    for output in outputs
                )
                status = DisplayStatus(
                    True,
                    "ready" if ready else "degraded",
                    "software_ready" if ready else "output_unavailable",
                )
        except ProbeError:
            status = DisplayStatus(True, "degraded", "output_unavailable")
    return Check(
        identifier, "ready" if status.state == "ready" else "degraded", status.reason
    )


def panel_tool(identifier):
    kind = Kind(identifier.removeprefix("panel_"))
    path = Path(TOOLS[kind])
    runtime, _, shared, _ = identities()
    try:
        info = path.stat()
        groups = set(os.getgrouplist("postcardscene", shared))
        executable = info.st_mode & (
            stat.S_IXUSR
            if info.st_uid == runtime
            else stat.S_IXGRP
            if info.st_gid in groups
            else stat.S_IXOTH
        )
        if not stat.S_ISREG(info.st_mode) or not executable:
            raise ValueError("Tool unavailable.")
    except (OSError, ValueError):
        return Check(identifier, "unavailable", "tool_unavailable")
    # Presence/executable DAC is the installed prerequisite only, not device or
    # panel support. No cec-ctl, ddcutil or wlopm invocation is needed for it.
    return Check(identifier, "ready", "software_ready")
