"""Fixed native host provisioning primitives; lifecycle ordering belongs to install.py."""

import grp
import io
import json
import os
import pwd
import signal
import stat
import subprocess
import zipfile
from pathlib import Path

SERVICES = tuple(
    f"postcardscene-{name}.service" for name in ("graphics", "runtime", "web")
)
ASSETS = {
    **{
        f"{name}/systemd/postcardscene-{name}.service": f"/etc/systemd/system/postcardscene-{name}.service"
        for name in ("graphics", "runtime", "web")
    },
    "graphics/pam.d/postcardscene-graphics": "/etc/pam.d/postcardscene-graphics",
}
CONFIG = b"""# Managed initial configuration; trusted host Python, never web input.
DATABASE_PATH = "/var/lib/postcardscene/postcardscene.sqlite3"
SESSION_SECRET_PATH = "/var/lib/postcardscene-web/session.key"
TRUSTED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
MEDIA_ALLOWED_ROOTS = ()
"""
ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
    "LANG": "C",
    "HOME": "/root",
}


class InstallError(Exception):
    """Only fixed safe reasons cross the installer output boundary."""


def command(args, *, interactive=False, user=None, timeout=300):
    options = {}
    environment = dict(ENV)
    if user:
        account = pwd.getpwnam(user)
        options.update(user=account.pw_uid, group=account.pw_gid, extra_groups=[])
        environment.update(
            HOME=account.pw_dir, POSTCARDSCENE_CONFIG="/etc/postcardscene/config.py"
        )
    with subprocess.Popen(
        args,
        env=environment,
        cwd="/",
        umask=0o007 if user else 0o022,
        stdin=None if interactive else subprocess.DEVNULL,
        stdout=None if interactive else subprocess.DEVNULL,
        stderr=None if interactive else subprocess.DEVNULL,
        start_new_session=not interactive,
        **options,
    ) as process:
        try:
            status = process.wait(timeout=timeout)
        except BaseException:
            if interactive:
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
        if status:
            raise InstallError("command_failed")


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
    for user, home in (
        ("postcardscene", "/var/lib/postcardscene"),
        ("postcardscene-web", "/var/lib/postcardscene-web"),
    ):
        run(
            (
                "/usr/sbin/useradd",
                "--system",
                "--gid",
                "postcardscene",
                "--no-create-home",
                "--home-dir",
                home,
                "--shell",
                "/usr/sbin/nologin",
                "--password",
                "!",
                user,
            )
        )
    groups = device_groups()
    if groups:
        run(
            (
                "/usr/sbin/usermod",
                "--append",
                "--groups",
                ",".join(groups),
                "postcardscene",
            )
        )
    runtime, web = pwd.getpwnam("postcardscene"), pwd.getpwnam("postcardscene-web")
    shared = grp.getgrnam("postcardscene").gr_gid
    if (
        runtime.pw_uid == web.pw_uid
        or 0 in (runtime.pw_uid, web.pw_uid)
        or runtime.pw_gid != shared
        or web.pw_gid != shared
        or os.getgrouplist(web.pw_name, shared) != [shared]
    ):
        raise InstallError("invalid_service_identities")
    return runtime.pw_uid, web.pw_uid, shared, grp.getgrnam("postcardscene-web").gr_gid


def install_packages(plan, run):
    run(("/usr/bin/apt-get", "-o", "DPkg::Lock::Timeout=60", "update"), timeout=600)
    run(
        (
            "/usr/bin/apt-get",
            "-o",
            "DPkg::Lock::Timeout=60",
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


def stage_payload(release, wheel_name, wheel, requirements, plan, run):
    directory(release)
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
            "d=m.distribution('postcardscene'); assert d.version == "
            + repr(release.name)
            + "; "
            "assert all(callable(e.load()) for e in d.entry_points if e.group == 'console_scripts'); "
            "assert all(f.locate().is_file() for f in d.files)",
        )
    )
    return python


def install_assets(wheel, run):
    with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
        for source, target in ASSETS.items():
            write_new(Path(target), archive.read("postcardscene/" + source))
    for name in ("runtime", "web"):
        dropin = Path(f"/etc/systemd/system/postcardscene-{name}.service.d")
        directory(dropin)
        write_new(dropin / "permissions.conf", b"[Service]\nUMask=0007\n")
    write_new(
        Path("/etc/tmpfiles.d/postcardscene.conf"),
        b"d /run/postcardscene 0750 postcardscene postcardscene -\n",
    )
    run(("/usr/bin/systemd-tmpfiles", "--create", "/etc/tmpfiles.d/postcardscene.conf"))


def reserve_graphics(preflight, run):
    # Record prior state before changing boot conflicts; no desktop file is replaced.
    states = {
        unit: service_state(preflight, unit)
        for unit in ("getty@tty1.service", "display-manager.service")
    }
    for unit, state in states.items():
        alias = Path("/etc/systemd/system") / unit
        state["local_symlink"] = str(alias.readlink()) if alias.is_symlink() else None
    write_new(
        Path("/opt/postcardscene/service-conflicts.json"),
        json.dumps(states, sort_keys=True).encode(),
    )
    for unit, state in states.items():
        if state["LoadState"] == "loaded":
            run(("/usr/bin/systemctl", "disable", "--now", unit))
    run(("/usr/bin/systemctl", "mask", "getty@tty1.service"))


def service_state(preflight, unit):
    try:
        return preflight.service_state(preflight.Host(), unit)
    except preflight.Rejected:
        raise InstallError("service_state_unavailable") from None


def require_stopped(preflight):
    if any(
        service_state(preflight, unit)["ActiveState"] != "inactive" for unit in SERVICES
    ):
        raise InstallError("services_must_be_stopped")


def bootstrap(python, run):
    # SQLite's initial 0644 creation mode cannot gain group write from umask.
    # Reserve only a new empty file as its runtime owner; Alembic owns all content.
    run(
        (
            python,
            "-I",
            "-c",
            "import os; fd=os.open('/var/lib/postcardscene/postcardscene.sqlite3', "
            "os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o660); os.close(fd)",
        ),
        user="postcardscene",
    )
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
