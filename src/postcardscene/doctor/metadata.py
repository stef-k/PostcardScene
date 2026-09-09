"""Exact managed installation metadata; never opens private signing authority."""

import email.parser
import grp
import importlib.metadata
import json
import os
import pwd
import re
import stat
from pathlib import Path

from . import Check

CONFIG = Path("/etc/postcardscene/config.py")
DATABASE = Path("/var/lib/postcardscene/postcardscene.sqlite3")
PACKAGE = Path(__file__).resolve().parents[1]


def metadata(path, uid, gid, mode, kind=stat.S_ISREG):
    info = path.lstat()
    if (
        not kind(info.st_mode)
        or info.st_uid != uid
        or info.st_gid != gid
        or stat.S_IMODE(info.st_mode) != mode
        or (kind != stat.S_ISDIR and info.st_nlink != 1)
    ):
        raise ValueError("Invalid installed authority.")
    # Ancestors must not redirect exact authority paths through local symlinks.
    if any(stat.S_ISLNK(parent.lstat().st_mode) for parent in path.parents):
        raise ValueError("Redirected installed authority.")
    return info


def read_regular(path, limit=65536):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError("Invalid bounded input.")
        result = stream.read(limit + 1)
        if len(result) > limit:
            raise ValueError("Oversized input.")
        return result


def identities():
    runtime = pwd.getpwnam("postcardscene")
    web = pwd.getpwnam("postcardscene-web")
    shared = grp.getgrnam("postcardscene").gr_gid
    private = grp.getgrnam("postcardscene-web").gr_gid
    if (
        runtime.pw_uid == web.pw_uid
        or min(runtime.pw_uid, web.pw_uid) <= 0
        or runtime.pw_gid != shared
        or web.pw_gid != shared
        or shared == private
        or min(shared, private) <= 0
        or set(os.getgrouplist(web.pw_name, shared)) - {shared, private}
    ):
        raise ValueError("Invalid service identities.")
    return runtime.pw_uid, web.pw_uid, shared, private


def release():
    active = Path("/opt/postcardscene/venv")
    info = active.lstat()
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
        raise ValueError("Invalid active release.")
    distribution = importlib.metadata.distribution("postcardscene")
    version = distribution.version
    if not re.fullmatch(r"[0-9][a-z0-9.]{0,63}", version):
        raise ValueError("Invalid version.")
    expected = Path("/opt/postcardscene/releases") / version / "venv"
    if active.resolve(strict=True) != expected or not PACKAGE.is_relative_to(expected):
        raise ValueError("Release identity mismatch.")
    for path in (
        Path("/opt/postcardscene"),
        expected.parent.parent,
        expected.parent,
        expected,
    ):
        metadata(path, 0, 0, 0o755, stat.S_ISDIR)
    if Path(distribution.locate_file("postcardscene")).resolve() != PACKAGE:
        raise ValueError("Distribution identity mismatch.")
    info = f"postcardscene-{version}.dist-info"
    headers = {}
    for name in ("METADATA", "WHEEL"):
        path = Path(distribution.locate_file(f"{info}/{name}"))
        metadata(path, 0, 0, 0o644)
        headers[name] = email.parser.BytesParser().parsebytes(read_regular(path))
    if (
        headers["METADATA"].get_all("Name") != ["postcardscene"]
        or headers["METADATA"].get_all("Version") != [version]
        or headers["WHEEL"].get_all("Root-Is-Purelib") != ["true"]
        or headers["WHEEL"].get_all("Tag") != ["py3-none-any"]
    ):
        raise ValueError("Invalid wheel metadata.")
    return (("version", version),)


def assets():
    for name in ("graphics", "runtime", "web"):
        metadata(Path(f"/etc/systemd/system/postcardscene-{name}.service"), 0, 0, 0o644)
    metadata(Path("/etc/pam.d/postcardscene-graphics"), 0, 0, 0o644)
    for name in ("runtime", "web"):
        path = Path(
            f"/etc/systemd/system/postcardscene-{name}.service.d/permissions.conf"
        )
        metadata(path, 0, 0, 0o644)
        if read_regular(path) != b"[Service]\nUMask=0007\n":
            raise ValueError("Invalid umask authority.")
    path = Path("/etc/tmpfiles.d/postcardscene.conf")
    metadata(path, 0, 0, 0o644)
    if (
        read_regular(path)
        != b"d /run/postcardscene 0750 postcardscene postcardscene -\n"
    ):
        raise ValueError("Invalid transient authority.")
    for name in ("rc.xml", "autostart"):
        metadata(PACKAGE / "graphics/labwc" / name, 0, 0, 0o644)


def conflicts():
    path = Path("/opt/postcardscene/service-conflicts.json")
    metadata(path, 0, 0, 0o644)
    record = json.loads(read_regular(path, 16384))
    if not isinstance(record, dict) or set(record) != {
        "getty@tty1.service",
        "display-manager.service",
    }:
        raise ValueError("Invalid conflict record.")
    for value in record.values():
        if not isinstance(value, dict) or set(value) != {
            "LoadState",
            "ActiveState",
            "UnitFileState",
            "local_symlink",
        }:
            raise ValueError("Invalid conflict state.")
        if (
            value["LoadState"] not in {"loaded", "not-found", "masked"}
            or value["ActiveState"] not in {"active", "inactive", "failed"}
            or value["UnitFileState"]
            not in {"enabled", "disabled", "static", "masked", "indirect", "alias", ""}
            or (
                value["local_symlink"] is not None
                and (
                    type(value["local_symlink"]) is not str
                    or len(value["local_symlink"]) > 4096
                )
            )
        ):
            raise ValueError("Invalid conflict values.")


def permissions(identifier):
    runtime, web, shared, private = identities()
    entries = {
        "config_authority": (
            (CONFIG.parent, 0, shared, 0o750, stat.S_ISDIR),
            (CONFIG, 0, shared, 0o640),
        ),
        "durable_permissions": (
            (DATABASE.parent, runtime, shared, 0o2770, stat.S_ISDIR),
        ),
        "private_key_permissions": (
            (Path("/var/lib/postcardscene-web"), web, private, 0o700, stat.S_ISDIR),
            (Path("/var/lib/postcardscene-web/session.key"), web, shared, 0o600),
        ),
        "cache_permissions": (
            (Path("/var/cache/postcardscene"), runtime, shared, 0o700, stat.S_ISDIR),
        ),
        "runtime_permissions": (
            (Path("/run/postcardscene"), runtime, shared, 0o750, stat.S_ISDIR),
        ),
        "wayland_permissions": (
            (Path("/run/postcardscene-wayland"), runtime, shared, 0o700, stat.S_ISDIR),
        ),
        "database_permissions": ((DATABASE, runtime, shared, 0o660),),
        "identities": (),
    }
    for entry in entries[identifier]:
        metadata(*entry)
    if identifier == "database_permissions":
        for suffix in ("-wal", "-shm"):
            path = Path(str(DATABASE) + suffix)
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            # Either authorized SQLite client can own an existing sidecar.
            if info.st_uid not in {runtime, web}:
                raise ValueError("Invalid sidecar owner.")
            metadata(path, info.st_uid, shared, 0o660)
    if identifier == "runtime_permissions":
        socket = Path("/run/postcardscene/panel-control.sock")
        try:
            socket.lstat()
        except FileNotFoundError:
            from .bounded import systemd_state
            from .data import configuration

            if (
                configuration().get("PANEL_POWER_RUNTIME_ENABLED") is True
                and systemd_state("postcardscene-runtime.service")["ActiveState"]
                == "active"
            ):
                raise ValueError("Live panel socket missing.")
            return
        metadata(socket, runtime, shared, 0o660, stat.S_ISSOCK)


def inspect(identifier):
    try:
        details = ()
        if identifier == "release":
            details = release()
        elif identifier == "installed_assets":
            assets()
        elif identifier == "conflict_record":
            conflicts()
        else:
            permissions(identifier)
        return Check(identifier, "ready", "ready", details)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        importlib.metadata.PackageNotFoundError,
    ):
        fatal = identifier in {"release", "config_authority", "identities"}
        return Check(identifier, "fatal" if fatal else "degraded", "authority_invalid")
