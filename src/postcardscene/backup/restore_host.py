"""Fixed installed-host authority and fail-stopped systemd restore boundary."""

import subprocess
import time
from pathlib import Path

from postcardscene.doctor import metadata

from .files import BackupError

SERVICES = tuple(
    f"postcardscene-{name}.service" for name in ("graphics", "runtime", "web")
)
AUXILIARY_UNITS = ("postcardscene-backup.service", "postcardscene-backup.timer")


def systemctl(*arguments):
    result = subprocess.run(
        ("/usr/bin/systemctl", *arguments),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=45,
        check=True,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    if len(result.stdout) > 65536:
        raise BackupError("restore_host_invalid")
    return result.stdout.decode("utf-8", errors="strict")


def state(unit):
    return dict(
        line.split("=", 1)
        for line in systemctl(
            "show", unit, "--property=LoadState,ActiveState,UnitFileState"
        ).splitlines()
    )


def immutable_authority():
    """Reuse read-only installed metadata, never doctor's current-data checks."""
    release = metadata.release()
    metadata.conflicts()
    metadata.assets()
    marker = Path("/opt/postcardscene/service-conflicts.json")
    result = [(str(marker), metadata.read_regular(marker))]
    packages = (
        "graphics/systemd",
        "runtime/systemd",
        "web/systemd",
        "backup/systemd",
        "backup/systemd",
    )
    for unit, package in zip((*SERVICES, *AUXILIARY_UNITS), packages, strict=True):
        target = Path("/etc/systemd/system") / unit
        metadata.metadata(target, 0, 0, 0o644)
        value = metadata.read_regular(target)
        if value != metadata.read_regular(metadata.PACKAGE / package / unit):
            raise BackupError("restore_host_invalid")
        result.append((str(target), value))
    return release, tuple(result)


def unit_authority():
    known = set((*SERVICES, *AUXILIARY_UNITS))
    for command in ("list-unit-files", "list-units"):
        output = systemctl(
            command, "postcardscene-*", "--all", "--no-legend", "--no-pager", "--plain"
        )
        if {line.split()[0] for line in output.splitlines() if line.strip()} - known:
            raise BackupError("restore_host_invalid")
    for unit in known:
        values = dict(
            line.split("=", 1)
            for line in systemctl(
                "show", unit, "--property=FragmentPath,DropInPaths,LoadState"
            ).splitlines()
        )
        expected = (
            []
            if unit == SERVICES[0] or unit in AUXILIARY_UNITS
            else [f"/etc/systemd/system/{unit}.d/permissions.conf"]
        )
        if (
            values.get("FragmentPath") != f"/etc/systemd/system/{unit}"
            or values.get("DropInPaths", "").split() != expected
            or values.get("LoadState") != "loaded"
        ):
            raise BackupError("restore_host_invalid")


def validate():
    identities = metadata.identities()
    immutable = immutable_authority()
    unit_authority()
    # These inspect only metadata. Damaged/empty current content is repairable.
    for name in (
        "config_authority",
        "durable_permissions",
        "private_key_permissions",
        "database_permissions",
    ):
        metadata.permissions(name)
    return identities, immutable


def require_quiescent(identities):
    deadline = time.monotonic() + 15
    while True:
        owned = False
        for path in Path("/proc").glob("[0-9]*/status"):
            try:
                value = metadata.read_regular(path)
            except FileNotFoundError:
                continue
            for line in value.splitlines():
                if line.startswith(b"Uid:") and set(
                    map(int, line.split()[1:])
                ).intersection(identities[:2]):
                    owned = True
        if not owned:
            return
        if time.monotonic() >= deadline:
            raise BackupError("restore_processes_remain")
        time.sleep(0.1)


def stop(identities):
    """Attempt every retirement action even if a preceding systemctl call fails."""
    failed = False
    actions = [
        ("stop", AUXILIARY_UNITS[1]),
        ("disable", AUXILIARY_UNITS[1]),
        ("stop", AUXILIARY_UNITS[0]),
    ]
    actions += [
        (action, unit) for unit in reversed(SERVICES) for action in ("stop", "disable")
    ]
    for action, unit in actions:
        try:
            systemctl(action, unit)
        except (Exception, KeyboardInterrupt):
            failed = True
    for unit in (*SERVICES, *AUXILIARY_UNITS):
        current = state(unit)
        expected = "static" if unit == AUXILIARY_UNITS[0] else "disabled"
        if (
            current.get("ActiveState") not in {"inactive", "failed"}
            or current.get("UnitFileState") != expected
        ):
            failed = True
    require_quiescent(identities)
    if failed:
        raise BackupError("restore_stop_failed")


def activate_services():
    for unit in SERVICES:
        systemctl("enable", unit)
    for unit in SERVICES:
        systemctl("start", unit)
        if state(unit) != {
            "LoadState": "loaded",
            "ActiveState": "active",
            "UnitFileState": "enabled",
        }:
            raise BackupError("restore_activation_failed")
    systemctl("enable", AUXILIARY_UNITS[1])


def activate_timer():
    systemctl("start", AUXILIARY_UNITS[1])
    if state(AUXILIARY_UNITS[1]) != {
        "LoadState": "loaded",
        "ActiveState": "active",
        "UnitFileState": "enabled",
    }:
        raise BackupError("restore_activation_failed")
