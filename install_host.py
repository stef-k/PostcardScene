"""Fixed native host provisioning primitives; lifecycle ordering belongs to install.py."""

import grp
import io
import json
import os
import pwd
import re
import shutil
import stat
import time
import zipfile
from pathlib import Path

from postcardscene_install_command import ENV as ENV
from postcardscene_install_command import command as command
from postcardscene_install_services import (
    AUXILIARY_UNITS,
    SERVICES,
    InstallError,
    require_auxiliary_installed,
    require_auxiliary_stopped,
    service_authority,
    service_state,
    stop_auxiliary,
)

ASSETS = {
    f"{name}/systemd/postcardscene-{name}.service": f"/etc/systemd/system/postcardscene-{name}.service"
    for name in ("graphics", "runtime", "web")
}
ASSETS.update(
    {
        f"backup/systemd/{unit}": f"/etc/systemd/system/{unit}"
        for unit in AUXILIARY_UNITS
    }
)
ASSETS["graphics/pam.d/postcardscene-graphics"] = "/etc/pam.d/postcardscene-graphics"
CONFIG = b"""# Managed initial configuration; trusted host Python, never web input.
DATABASE_PATH = "/var/lib/postcardscene/postcardscene.sqlite3"
SESSION_SECRET_PATH = "/var/lib/postcardscene-web/session.key"
TRUSTED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
MEDIA_ALLOWED_ROOTS = ()
"""


def trusted_parent(path):
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise InstallError("unsafe_destination_parent")


def directory(path, uid=0, gid=0, mode=0o755):
    trusted_parent(path)
    path.mkdir(mode=0o700)
    os.chown(path, uid, gid)
    path.chmod(mode)
    return path.stat()


def write_new(path, content, mode=0o644, gid=0):
    trusted_parent(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fchown(stream.fileno(), 0, gid)
        os.fchmod(stream.fileno(), mode)
        os.fsync(stream.fileno())


def device_groups():
    selected = set()
    for pattern, allowed in (
        ("dri/*", {"render", "video"}),
        ("snd/*", {"audio"}),
        ("cec*", {"video"}),
        ("i2c-*", {"i2c"}),
    ):
        for device in Path("/dev").glob(pattern):
            info = device.lstat()
            if (
                not stat.S_ISCHR(info.st_mode)
                or info.st_uid != 0
                or info.st_mode & 0o060 != 0o060
            ):
                continue
            name = grp.getgrgid(info.st_gid).gr_name
            if name in allowed:
                selected.add(name)
    return tuple(sorted(selected))


def provision_identities(run):
    for group in ("postcardscene", "postcardscene-web"):
        run(("/usr/sbin/groupadd", "--system", group))
    useradd = (
        "/usr/sbin/useradd",
        "--system",
        "--gid=postcardscene",
        "--no-create-home",
        "--shell=/usr/sbin/nologin",
        "--password=!",
    )
    for user, home in (
        ("postcardscene", "/var/lib/postcardscene"),
        ("postcardscene-web", "/var/lib/postcardscene-web"),
    ):
        run((*useradd, "--home-dir", home, user))
    groups = device_groups()
    if groups:
        members = ",".join(groups)
        run(("/usr/sbin/usermod", "--append", "--groups", members, "postcardscene"))
    return preserved_identities()


def install_packages(plan, run):
    apt = ("/usr/bin/apt-get", "-o", "DPkg::Lock::Timeout=60")
    run((*apt, "update"), timeout=600)
    run(
        (
            *apt,
            "--yes",
            "--no-install-recommends",
            "install",
            *plan.apt_packages,
            *plan.seat_packages,
        ),
        timeout=900,
    )
    if plan.snap and any(
        tool.executable == "/snap/bin/chromium" and tool.state == "install_required"
        for tool in plan.tools
    ):
        run(
            ("/usr/bin/snap", "install", "chromium", "--channel=latest/stable"),
            timeout=600,
        )


def stage_payload(
    release, wheel_name, wheel, requirements, plan, run, *, on_created=None
):
    created = directory(release)
    if on_created is not None:
        on_created(created)
    write_new(release / wheel_name, wheel)
    write_new(release / "runtime-requirements.txt", requirements)
    venv = release / "venv"
    run((plan.python, "-I", "-m", "venv", str(venv)))
    python = str(venv / "bin/python")
    pip = (python, "-I", "-m", "pip", "--isolated", "--disable-pip-version-check")
    run(
        (
            *pip,
            "install",
            "--no-cache-dir",
            "--require-hashes",
            "--only-binary=:all:",
            "-r",
            str(release / "runtime-requirements.txt"),
        ),
        timeout=600,
    )
    run((*pip, "install", "--no-cache-dir", "--no-deps", str(release / wheel_name)))
    run((*pip, "check"))
    run(
        (
            python,
            "-I",
            "-c",
            "import importlib.metadata as m; import postcardscene.web, postcardscene.runtime; "
            f"d=m.distribution('postcardscene'); assert d.version == {release.name!r}; "
            "assert all(callable(e.load()) for e in d.entry_points if e.group == 'console_scripts'); "
            "assert all(f.locate().is_file() for f in d.files)",
        )
    )
    return python


def install_assets(wheel, run):
    for dropin in DROPINS:
        directory(dropin)
    for path, content in asset_bytes(wheel).items():
        write_new(path, content)
    run(("/usr/bin/systemd-tmpfiles", "--create", str(TMPFILES)))


def reserve_graphics(preflight, run, *, preserved=False):
    # Record prior state before changing boot conflicts; no desktop file is replaced.
    states = {unit: service_state(preflight, unit) for unit in CONFLICTS}
    for unit, state in states.items():
        alias = Path("/etc/systemd/system") / unit
        state["local_symlink"] = str(alias.readlink()) if alias.is_symlink() else None
    destination = MARKER.with_suffix(".new") if preserved else MARKER
    if preserved:
        conflict_record(preflight)
    write_new(destination, json.dumps(states, sort_keys=True).encode())
    if preserved:
        os.replace(destination, MARKER)
    for unit, state in states.items():
        if state["LoadState"] == "loaded":
            run(("/usr/bin/systemctl", "disable", "--now", unit))
    run(("/usr/bin/systemctl", "mask", "getty@tty1.service"))


def require_stopped(preflight):
    for unit in SERVICES:
        if service_state(preflight, unit)["ActiveState"] != "inactive":
            raise InstallError("services_must_be_stopped")


def bootstrap(python, run):
    # SQLite's initial 0644 creation mode cannot gain group write from umask.
    # Reserve only a new empty file as its runtime owner; Alembic owns all content.
    reserve_database = (
        "import os; fd=os.open('/var/lib/postcardscene/postcardscene.sqlite3', "
        "os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o660); os.close(fd)"
    )
    run((python, "-I", "-c", reserve_database), user="postcardscene")
    cli = (python, "-I", "-m", "flask", "--app", "postcardscene.web:create_app")
    run((*cli, "auth", "init-secret"), user="postcardscene-web")
    run((*cli, "db", "upgrade"), user="postcardscene-web")
    run((*cli, "db", "check"), user="postcardscene-web")
    run(
        (*cli, "auth", "create-admin"),
        user="postcardscene-web",
        interactive=True,
        timeout=900,
    )


def activate_payload(release):
    # Initial install only: atomic creation refuses any existing target.
    Path("/opt/postcardscene/venv").symlink_to(release / "venv")


def discard_staged_payload(release):
    # Caller owns this exact fresh root-controlled release, never durable state.
    metadata(release, mode=0o755, kind=stat.S_ISDIR)
    validate_tree(release, 0, 0, payload=True)
    shutil.rmtree(release)


def discard_reinstall_staging(release, created_parent):
    # Only the parent exclusively created by this reinstall grants cleanup scope.
    trusted_parent(RELEASES)
    current = metadata(RELEASES, mode=0o755, kind=stat.S_ISDIR)
    if not os.path.samestat(current, created_parent) or release.parent != RELEASES:
        raise InstallError("reinstall_cleanup_uncertain")
    if os.path.lexists(ROOT / "venv") or set(RELEASES.iterdir()) - {release}:
        raise InstallError("reinstall_cleanup_uncertain")
    validate_tree(RELEASES, 0, 0, payload=True)
    if os.path.lexists(release):
        discard_staged_payload(release)
    RELEASES.rmdir()


# Closed lifecycle paths; durable roots are inspected, never recursively repaired.
ROOT = Path("/opt/postcardscene")
RELEASES = ROOT / "releases"
MARKER = ROOT / "service-conflicts.json"
DATABASE = Path("/var/lib/postcardscene/postcardscene.sqlite3")
KEY = Path("/var/lib/postcardscene-web/session.key")
CONFLICTS = ("getty@tty1.service", "display-manager.service")
DROPINS = tuple(
    Path(f"/etc/systemd/system/postcardscene-{n}.service.d") for n in ("runtime", "web")
)
TMPFILES = Path("/etc/tmpfiles.d/postcardscene.conf")
TRANSIENTS = (Path("/run/postcardscene"), Path("/run/postcardscene-wayland"))
CACHE = Path("/var/cache/postcardscene")


def metadata(path, uid=0, gid=0, mode=0o644, kind=stat.S_ISREG):
    info = path.lstat()
    if (
        not kind(info.st_mode)
        or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (uid, gid, mode)
        or (kind != stat.S_ISDIR and info.st_nlink != 1)
    ):
        raise InstallError("managed_authority_invalid")
    if any(stat.S_ISLNK(p.lstat().st_mode) for p in path.parents):
        raise InstallError("managed_authority_invalid")
    return info


def read_regular(path, limit=65536):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise InstallError("managed_authority_invalid")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise InstallError("managed_authority_invalid")
        return data


def preserved_identities():
    shadow = dict(
        line.split(":", 2)[:2]
        for line in read_regular(Path("/etc/shadow")).decode().splitlines()
    )

    runtime, web = pwd.getpwnam("postcardscene"), pwd.getpwnam("postcardscene-web")
    shared, private = grp.getgrnam("postcardscene"), grp.getgrnam("postcardscene-web")
    if (
        runtime.pw_uid == web.pw_uid
        or min(runtime.pw_uid, web.pw_uid, shared.gr_gid, private.gr_gid) <= 0
        or shared.gr_gid == private.gr_gid
        or private.gr_mem
        or set(shared.gr_mem) - {runtime.pw_name, web.pw_name}
        or os.getgrouplist(web.pw_name, shared.gr_gid) != [shared.gr_gid]
    ):
        raise InstallError("managed_authority_invalid")
    for account, home in ((runtime, DATABASE.parent), (web, KEY.parent)):
        if (
            account.pw_gid != shared.gr_gid
            or account.pw_dir != str(home)
            or account.pw_shell != "/usr/sbin/nologin"
            or not shadow[account.pw_name].startswith(("!", "*"))
        ):
            raise InstallError("managed_authority_invalid")
    allowed = {shared.gr_gid} | {
        g.gr_gid
        for g in grp.getgrall()
        if g.gr_name in {"render", "video", "audio", "i2c"}
    }
    if set(os.getgrouplist(runtime.pw_name, shared.gr_gid)) - allowed:
        raise InstallError("managed_authority_invalid")
    return runtime.pw_uid, web.pw_uid, shared.gr_gid, private.gr_gid


def preserved_authority():
    import ast

    runtime, web, shared, private = preserved_identities()
    for path, uid, gid, mode in (
        (ROOT, 0, 0, 0o755),
        (Path("/etc/postcardscene"), 0, shared, 0o750),
        (DATABASE.parent, runtime, shared, 0o2770),
        (KEY.parent, web, private, 0o700),
    ):
        trusted_parent(path)
        metadata(path, uid, gid, mode, stat.S_ISDIR)
    config = Path("/etc/postcardscene/config.py")
    metadata(config, 0, shared, 0o640)
    # Match doctor's non-executing configuration boundary before privileged work.
    values = {}
    for node in ast.parse(read_regular(config)).body:
        if (
            not isinstance(node, ast.Assign)
            or len(node.targets) != 1
            or not isinstance(node.targets[0], ast.Name)
        ):
            raise InstallError("managed_authority_invalid")
        values[node.targets[0].id] = ast.literal_eval(node.value)
    if values.get("DATABASE_PATH", str(DATABASE)) != str(DATABASE) or values.get(
        "SESSION_SECRET_PATH", str(KEY)
    ) != str(KEY):
        raise InstallError("managed_authority_invalid")
    metadata(DATABASE, runtime, shared, 0o660)
    for suffix in ("-wal", "-shm"):
        path = Path(str(DATABASE) + suffix)
        if os.path.lexists(path):
            info = path.lstat()
            if info.st_uid not in (runtime, web):
                raise InstallError("managed_authority_invalid")
            metadata(path, info.st_uid, shared, 0o660)
    if metadata(KEY, web, shared, 0o600).st_size != 32:
        raise InstallError("managed_authority_invalid")
    return runtime, web, shared, private


def conflict_record(preflight):
    metadata(MARKER)
    record = json.loads(read_regular(MARKER, 16384))
    if not isinstance(record, dict) or set(record) != set(CONFLICTS):
        raise InstallError("conflict_record_invalid")
    fields = {"LoadState", "ActiveState", "UnitFileState", "local_symlink"}
    for state in record.values():
        if not isinstance(state, dict) or set(state) != fields:
            raise InstallError("conflict_record_invalid")
        if (
            state["LoadState"] not in {"loaded", "not-found", "masked"}
            or state["ActiveState"] not in {"active", "inactive", "failed"}
            or state["UnitFileState"] not in preflight.UNIT_FILE_STATES
        ):
            raise InstallError("conflict_record_invalid")
        target = state["local_symlink"]
        if target is not None and (
            type(target) is not str
            or not target
            or len(target) > 4096
            or "\x00" in target
        ):
            raise InstallError("conflict_record_invalid")
    return record


def asset_bytes(wheel):
    with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
        assets = {
            Path(target): archive.read("postcardscene/" + source)
            for source, target in ASSETS.items()
        }
    assets.update({p / "permissions.conf": b"[Service]\nUMask=0007\n" for p in DROPINS})
    assets[TMPFILES] = b"d /run/postcardscene 0750 postcardscene postcardscene -\n"
    return assets


def validate_tree(root, uid, gid, *, payload=False, allow_missing_links=False):
    # No mount crossing, hardlinks, devices or service-controlled symlink traversal.
    for line in (
        read_regular(Path("/proc/self/mountinfo"), 1024 * 1024).decode().splitlines()
    ):
        mounted = Path(
            re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), line.split()[4])
        )
        if mounted == root or mounted.is_relative_to(root):
            raise InstallError("unsafe_removal_tree")
    device = root.lstat().st_dev
    count = 0
    for parent, directories, files in os.walk(root, followlinks=False):
        for path in (Path(parent), *(Path(parent) / n for n in directories + files)):
            count += 1
            info = path.lstat()
            if count > 100000 or (info.st_dev, info.st_uid, info.st_gid) != (
                device,
                uid,
                gid,
            ):
                raise InstallError("unsafe_removal_tree")
            if stat.S_ISLNK(info.st_mode) and payload:
                resolved = path.resolve(strict=not allow_missing_links)
                if not resolved.is_relative_to(root) and not (
                    path.parent.name == "bin"
                    and path.name.startswith("python")
                    and resolved.parent == Path("/usr/bin")
                ):
                    raise InstallError("unsafe_removal_tree")
            elif not (
                stat.S_ISDIR(info.st_mode)
                or stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
            ):
                raise InstallError("unsafe_removal_tree")
            if payload and not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022:
                raise InstallError("unsafe_removal_tree")


def validate_asset(path, content):
    trusted_parent(path)
    metadata(path)
    if read_regular(path) != content:
        raise InstallError("managed_authority_invalid")


def installed_authority(version, wheel, *, extra_releases=(), extra_roots=()):
    runtime, _, shared, _ = preserved_authority()
    release = RELEASES / version
    if set(RELEASES.iterdir()) != {release, *extra_releases}:
        raise InstallError("managed_authority_invalid")
    for path in (RELEASES, release, release / "venv"):
        metadata(path, mode=0o755, kind=stat.S_ISDIR)
    active = ROOT / "venv"
    metadata(active, mode=0o777, kind=stat.S_ISLNK)
    if active.readlink() != release / "venv":
        raise InstallError("managed_authority_invalid")
    wheel_path = release / f"postcardscene-{version}-py3-none-any.whl"
    metadata(wheel_path)
    if read_regular(wheel_path, 32 * 1024 * 1024) != wheel:
        raise InstallError("managed_authority_invalid")
    validate_tree(release, 0, 0, payload=True)
    for path, content in asset_bytes(wheel).items():
        validate_asset(path, content)
    for path in DROPINS:
        metadata(path, mode=0o755, kind=stat.S_ISDIR)
        if set(path.iterdir()) != {path / "permissions.conf"}:
            raise InstallError("managed_authority_invalid")
    metadata(CACHE, runtime, shared, 0o700, stat.S_ISDIR)
    for path, mode in zip(TRANSIENTS, (0o750, 0o700), strict=True):
        # systemd removes RuntimeDirectory when graphics is stopped.
        if os.path.lexists(path):
            metadata(path, runtime, shared, mode, stat.S_ISDIR)
    if set(ROOT.iterdir()) != {MARKER, RELEASES, active, *extra_roots}:
        raise InstallError("managed_authority_invalid")


def owned_processes(identities):
    owned = set(identities[:2])
    for path in Path("/proc").glob("[0-9]*/status"):
        try:
            status = read_regular(path).decode()
        except FileNotFoundError:
            continue
        for line in status.splitlines():
            if line.startswith("Uid:") and owned.intersection(
                map(int, line.split()[1:])
            ):
                return True

    return False


def require_no_processes(identities):
    deadline = time.monotonic() + 15
    while owned_processes(identities):
        if time.monotonic() >= deadline:
            raise InstallError("owned_processes_remain")
        time.sleep(0.1)


def classify(version, wheel, preflight):
    try:
        roots = tuple(Path(p) for p in preflight.ROOTS)
        assets = (*map(Path, ASSETS.values()), *DROPINS, TMPFILES)
        present = any(os.path.lexists(p) for p in (*roots, *assets))
        names = {p.pw_name for p in pwd.getpwall()} | {
            g.gr_name for g in grp.getgrall()
        }
        if not present and not names.intersection(
            {"postcardscene", "postcardscene-web"}
        ):
            preflight.service_check(preflight.Host(), None)
            return "clean"
        identities = preserved_authority()
        conflict_record(preflight)
        absent = (ROOT / "venv", RELEASES, *assets, CACHE, *TRANSIENTS)
        if not any(os.path.lexists(p) for p in absent):
            if set(ROOT.iterdir()) != {MARKER}:
                return "partial_or_unknown"
            preflight.service_check(preflight.Host(), None, "preserved")
            require_no_processes(identities)
            return "removed_preserved"
        installed_authority(version, wheel)
        service_authority(preflight)
        require_auxiliary_installed(preflight)
        return "installed_managed"
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        SyntaxError,
        InstallError,
        preflight.Rejected,
    ):
        return "partial_or_unknown"


def restore_graphics(preflight, run, record):
    for unit, state in record.items():
        alias = Path("/etc/systemd/system") / unit
        if os.path.lexists(alias):
            if not alias.is_symlink() or alias.lstat().st_uid != 0:
                raise InstallError("conflict_restore_uncertain")
            target = str(alias.readlink())
            if target not in ("/dev/null", state["local_symlink"]):
                raise InstallError("conflict_restore_uncertain")
            alias.unlink()
        masked_active = (
            state["ActiveState"] == "active" and state["local_symlink"] == "/dev/null"
        )
        if state["local_symlink"] is not None and not masked_active:
            alias.symlink_to(state["local_symlink"])
        run(("/usr/bin/systemctl", "daemon-reload"))
        enabled = state["UnitFileState"]
        if enabled in ("enabled", "enabled-runtime"):
            args = ("--runtime",) if enabled == "enabled-runtime" else ()
            run(("/usr/bin/systemctl", "enable", *args, unit))
        if state["ActiveState"] == "active":
            run(("/usr/bin/systemctl", "start", unit))
        if masked_active:
            alias.symlink_to("/dev/null")
            run(("/usr/bin/systemctl", "daemon-reload"))
        if service_state(preflight, unit) != {
            k: v for k, v in state.items() if k != "local_symlink"
        }:
            raise InstallError("conflict_restore_uncertain")


def remove_transients(identities):
    runtime, _, shared, _ = identities
    for path, mode in zip(TRANSIENTS, (0o750, 0o700), strict=True):
        if not os.path.lexists(path):
            continue
        metadata(path, runtime, shared, mode, stat.S_ISDIR)
        # Owners should remove their sockets/locks on stop. Preserve any residue
        # rather than guessing whether an arbitrary socket is safe to unlink.
        path.rmdir()
    metadata(CACHE, runtime, shared, 0o700, stat.S_ISDIR)
    validate_tree(CACHE, runtime, shared)
    shutil.rmtree(CACHE)


def remove_managed(version, wheel, preflight, run):
    installed_authority(version, wheel)
    service_authority(preflight)
    record = conflict_record(preflight)
    identities = preserved_authority()
    stop_auxiliary(run)
    require_auxiliary_stopped(preflight)
    run(("/usr/bin/systemctl", "stop", *reversed(SERVICES)))
    run(("/usr/bin/systemctl", "disable", *SERVICES))
    require_stopped(preflight)
    require_no_processes(identities)
    restore_graphics(preflight, run, record)
    # Revalidate immediately before deleting each exact authority.
    installed_authority(version, wheel)
    (ROOT / "venv").unlink()
    release = RELEASES / version
    discard_staged_payload(release)
    for path, content in asset_bytes(wheel).items():
        validate_asset(path, content)
        path.unlink()
    for path in DROPINS:
        path.rmdir()
    run(("/usr/bin/systemctl", "daemon-reload"))
    remove_transients(identities)
    RELEASES.rmdir()
