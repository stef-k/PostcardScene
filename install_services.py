"""Exact managed systemd authority and retirement primitives; authenticated by install.py."""

import subprocess

SERVICES = tuple(
    f"postcardscene-{name}.service" for name in ("graphics", "runtime", "web")
)
AUXILIARY_UNITS = ("postcardscene-backup.service", "postcardscene-backup.timer")


class InstallError(Exception):
    """Only fixed safe reasons cross the installer output boundary."""


def service_state(preflight, unit):
    try:
        return preflight.service_state(preflight.Host(), unit)
    except preflight.Rejected:
        raise InstallError("service_state_unavailable") from None


def service_authority(preflight):
    observer = preflight.Host()
    if preflight.unit_names(observer) - set((*SERVICES, *AUXILIARY_UNITS)):
        raise InstallError("managed_service_authority_invalid")
    for unit in (*SERVICES, *AUXILIARY_UNITS):
        status, output = observer.command(
            ("/usr/bin/systemctl", "show", unit, "--property=FragmentPath,DropInPaths")
        )
        values = dict(line.split("=", 1) for line in output.splitlines())
        expected = (
            []
            if unit == SERVICES[0] or unit in AUXILIARY_UNITS
            else [f"/etc/systemd/system/{unit}.d/permissions.conf"]
        )
        if (
            status
            or values.get("FragmentPath") != f"/etc/systemd/system/{unit}"
            or values.get("DropInPaths", "").split() != expected
        ):
            raise InstallError("managed_service_authority_invalid")


def stop_auxiliary(run):
    # Retire the trigger first so no new oneshot can race process quiescence.
    # Attempt every action even when one fails; recovery must still disable it.
    failed = False
    for action, unit in (
        ("stop", AUXILIARY_UNITS[1]),
        ("disable", AUXILIARY_UNITS[1]),
        ("stop", AUXILIARY_UNITS[0]),
    ):
        try:
            run(("/usr/bin/systemctl", action, unit))
        except (OSError, RuntimeError, InstallError, subprocess.SubprocessError):
            failed = True
    if failed:
        raise InstallError("auxiliary_stop_failed")


def require_auxiliary_stopped(preflight):
    for unit in AUXILIARY_UNITS:
        state = service_state(preflight, unit)
        if state["ActiveState"] != "inactive" or state["UnitFileState"] not in (
            "disabled",
            "static",
            "",
        ):
            raise InstallError("auxiliary_stop_failed")


def require_auxiliary_installed(preflight):
    timer = service_state(preflight, AUXILIARY_UNITS[1])
    service = service_state(preflight, AUXILIARY_UNITS[0])
    if (
        timer
        != {"LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled"}
        or service["LoadState"] != "loaded"
        or service["UnitFileState"] != "static"
    ):
        raise InstallError("managed_service_authority_invalid")
