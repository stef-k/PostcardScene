"""Deliberate clean native installation; final published-bundle verification is #143."""

import argparse
import base64
import configparser
import csv
import email.parser
import grp
import hashlib
import importlib.util
import io
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

# Reviewed #138/#139 input pins, checked against source by release-input validation.
# These are installer inputs, not a release manifest or published-bundle schema.
INPUT_HASHES = {
    "install_preflight.py": "2dfd1d5df53ec933ed4fb38a1edbe4309b5dd4cff901a9a5de81a5c6ff050c57",
    "runtime-requirements.txt": "ca8eb8d430bd3d883523e592c99bec74c65c7537a765c52998001f0ae4c76d3a",
}
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


def read_input(path, limit=32 * 1024 * 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise InstallError("invalid_bundle_input")
        return stream.read(limit + 1)


def validate_wheel(data, filename):
    match = re.fullmatch(r"postcardscene-([0-9][a-z0-9.]*)-py3-none-any.whl", filename)
    if not match:
        raise InstallError("invalid_wheel_identity")
    version = match[1]
    info = f"postcardscene-{version}.dist-info"
    with zipfile.ZipFile(io.BytesIO(data)) as wheel:
        names = wheel.namelist()
        if (
            len(names) != len(set(names))
            or sum(i.file_size for i in wheel.infolist()) > 64 * 1024 * 1024
        ):
            raise InstallError("invalid_wheel_members")
        for name in names:
            if (
                not name.startswith(("postcardscene/", info + "/"))
                or ".." in name.split("/")
                or "\\" in name
                or name.endswith("/")
            ):
                raise InstallError("invalid_wheel_members")
        metadata = email.parser.BytesParser().parsebytes(wheel.read(f"{info}/METADATA"))
        tags = email.parser.BytesParser().parsebytes(wheel.read(f"{info}/WHEEL"))
        if (
            metadata["Name"] != "postcardscene"
            or metadata["Version"] != version
            or set(metadata["Requires-Python"].replace(" ", "").split(","))
            != {">=3.11", "<3.15"}
            or tags["Root-Is-Purelib"] != "true"
            or tags.get_all("Tag") != ["py3-none-any"]
        ):
            raise InstallError("invalid_wheel_metadata")
        entries = configparser.ConfigParser()
        entries.read_string(wheel.read(f"{info}/entry_points.txt").decode())
        if dict(entries["console_scripts"]) != {
            "postcardscene-web": "postcardscene.web.server:main",
            "postcardscene-runtime": "postcardscene.runtime.cli:main",
            "postcardscene-input-emitter": "postcardscene.graphics.local_input:main",
        }:
            raise InstallError("invalid_wheel_entry_points")
        validate_record(wheel, info)
        for name in (
            *ASSETS,
            "graphics/labwc/rc.xml",
            "graphics/labwc/autostart",
            "migrations/env.py",
        ):
            wheel.read("postcardscene/" + name)
    return version


def validate_record(wheel, info):
    record = f"{info}/RECORD"
    rows = list(csv.reader(io.StringIO(wheel.read(record).decode())))
    if len(rows) != len(wheel.namelist()) or {r[0] for r in rows} != set(
        wheel.namelist()
    ):
        raise InstallError("invalid_wheel_record")
    for name, digest, size in rows:
        if name == record:
            if digest or size:
                raise InstallError("invalid_wheel_record")
            continue
        content = wheel.read(name)
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(content).digest())
            .decode()
            .rstrip("=")
        )
        if digest != "sha256=" + expected or size != str(len(content)):
            raise InstallError("invalid_wheel_record")


def validate_inputs(bundle):
    # The operator trusts the reviewed installer itself. Authenticate support before
    # importing it; never execute an unverified sibling module during validation.
    inputs = {name: read_input(bundle / name) for name in INPUT_HASHES}
    for name, digest in INPUT_HASHES.items():
        if hashlib.sha256(inputs[name]).hexdigest() != digest:
            raise InstallError("install_support_mismatch")
    wheels = list(bundle.glob("*.whl"))
    if len(wheels) != 1:
        raise InstallError("expected_one_application_wheel")
    data = read_input(wheels[0])
    version = validate_wheel(data, wheels[0].name)
    spec = importlib.util.spec_from_loader(
        "postcardscene_install_preflight", loader=None
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    exec(
        compile(inputs["install_preflight.py"], "install_preflight.py", "exec"),
        module.__dict__,
    )
    return version, wheels[0].name, data, inputs["runtime-requirements.txt"], module


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
    if plan.snap:
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
        unit: preflight.service_state(preflight.Host(), unit)
        for unit in ("getty@tty1.service", "display-manager.service")
    }
    write_new(
        Path("/opt/postcardscene/service-conflicts.json"),
        json.dumps(states, sort_keys=True).encode(),
    )
    for unit, state in states.items():
        if state["LoadState"] == "loaded":
            run(("/usr/bin/systemctl", "disable", "--now", unit))
    run(("/usr/bin/systemctl", "mask", "getty@tty1.service"))


def bootstrap(python, run):
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


class Installation:
    def __init__(self, bundle, run=command):
        self.bundle = bundle
        self.run = run
        self.phase = "validation"
        self.release = None
        self.durable = False
        self.activation = False

    def install(self):
        version, wheel_name, wheel, requirements, preflight = validate_inputs(
            self.bundle
        )
        result = preflight.preflight()
        if not result.ok:
            raise InstallError("preflight_rejected")
        if os.geteuid() != 0 or not sys.stdin.isatty():
            raise InstallError("root_interactive_terminal_required")
        self.phase = "packages"
        install_packages(result.plan, self.run)
        # Repeat the read-only clean-host gate after package changes, before state.
        result = preflight.preflight()
        if not result.ok or any(t.state != "installed" for t in result.plan.tools):
            raise InstallError("post_package_preflight_rejected")
        self.phase = "payload"
        directory(Path("/opt/postcardscene"))
        directory(Path("/opt/postcardscene/releases"))
        self.release = Path("/opt/postcardscene/releases") / version
        python = stage_payload(
            self.release, wheel_name, wheel, requirements, result.plan, self.run
        )
        self.phase = "identities_and_roots"
        runtime, web, shared, private = provision_identities(self.run)
        directory(Path("/etc/postcardscene"), gid=shared, mode=0o750)
        write_new(Path("/etc/postcardscene/config.py"), CONFIG, 0o640, shared)
        directory(Path("/var/lib/postcardscene"), runtime, shared, 0o2770)
        directory(Path("/var/lib/postcardscene-web"), web, private, 0o700)
        directory(Path("/var/cache/postcardscene"), runtime, shared, 0o700)
        self.phase = "assets"
        install_assets(wheel, self.run)
        reserve_graphics(preflight, self.run)
        self.run(("/usr/bin/systemctl", "stop", *SERVICES))
        self.phase = "bootstrap"
        self.durable = True
        bootstrap(python, self.run)
        self.phase = "activation"
        # Initial install only: symlink creation is atomic and refuses any target.
        Path("/opt/postcardscene/venv").symlink_to(self.release / "venv")
        self.activation = True
        self.run(("/usr/bin/systemctl", "daemon-reload"))
        self.run(("/usr/bin/systemctl", "enable", *SERVICES))
        for unit in SERVICES:
            self.run(("/usr/bin/systemctl", "start", unit))
        self.run(("/usr/bin/systemctl", "is-active", "--quiet", *SERVICES))
        self.phase = "complete"

    def recover(self):
        if self.durable:
            try:
                self.run(("/usr/bin/systemctl", "stop", *SERVICES))
            except (OSError, InstallError, subprocess.SubprocessError):
                print(
                    "Service stop failed; inspect and stop installed units before recovery.",
                    file=sys.stderr,
                )
        elif (
            self.release is not None
            and self.release.is_dir()
            and not self.release.is_symlink()
        ):
            # Exact fresh root-controlled payload only; never durable/config authority.
            shutil.rmtree(self.release)
        print(
            f"Install failed during {self.phase}. Preserve config, state and signing key. "
            "Inspect host package/service status and the managed-install recovery instructions; "
            "do not delete state or rerun as an update.",
            file=sys.stderr,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install",))
    parser.parse_args()
    operation = Installation(Path(__file__).resolve().parent)
    try:
        operation.install()
    except (
        InstallError,
        OSError,
        ValueError,
        KeyError,
        zipfile.BadZipFile,
        subprocess.SubprocessError,
        KeyboardInterrupt,
    ):
        operation.recover()
        return 1
    print(
        "Installation complete; services active. Physical display readiness is unverified."
    )
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
