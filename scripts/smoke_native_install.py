"""Privileged disposable Linux CI only: real installed layout, two UIDs and units.

This bypasses ARM64/package detection deliberately, exercising provisioning steps
on an x86 CI VM. It never establishes a supported managed or physical target.
"""

import importlib.util
import json
import os
import pwd
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from http.client import HTTPConnection
from pathlib import Path
from threading import Event
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
PYTHON = "/opt/postcardscene/venv/bin/python"
STATE = Path("/var/lib/postcardscene")
KEY = Path("/var/lib/postcardscene-web/session.key")
SOCKET = Path("/run/postcardscene/panel-control.sock")
DEVICE = Path("/run/postcardscene-device-smoke")


def worker(action):
    from sqlalchemy import select

    from postcardscene.persistence import Database
    from postcardscene.runtime.panel_control import PanelControl
    from postcardscene.session_secret import read_secret
    from postcardscene.settings import ApplicationSettings, set_timezone

    database = STATE / "postcardscene.sqlite3"
    if action == "backup":
        backup_smoke()
    elif action == "doctor":
        doctor_layout()
    elif action == "socket":
        server = PanelControl(None, Event())
        server.start()
        try:
            print("ready", flush=True)
            input()
        finally:
            server.stop()
    elif action == "web-access":
        assert len(read_secret(KEY)) == 32
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
            client.settimeout(2)
            client.connect(str(SOCKET))
            client.sendall(b'{"version":1,"action":"status"}')
            assert json.loads(client.recv(1024))["outcome"] == "unavailable"
        for operation in (
            lambda: SOCKET.unlink(),
            lambda: (SOCKET.parent / "foreign").write_text("forbidden"),
            lambda: list(Path("/run/postcardscene-wayland").iterdir()),
            lambda: DEVICE.open("rb"),
        ):
            denied(operation)
    elif action == "runtime-private":
        denied(lambda: KEY.read_bytes())
    elif action == "write":
        set_timezone(Database(database), "Europe/Athens")
    else:
        db = Database(database)
        set_timezone(db, "UTC")
        with db.transaction() as session:
            assert session.scalar(select(ApplicationSettings.timezone)) == "UTC"
            print("ready", flush=True)
            input()
        db.engine.dispose()


def backup_smoke():
    """Exercise the shipped CLI under the actual managed web UID and DAC."""
    import tarfile

    with tempfile.TemporaryDirectory(
        prefix="postcardscene-backup-smoke-", dir="/tmp"
    ) as temp:
        command = ["/opt/postcardscene/venv/bin/postcardscene-backup"]

        def invoke(*args):
            result = subprocess.run(
                command + list(args), capture_output=True, check=True, timeout=15
            )
            return json.loads(result.stdout)

        identity = invoke("create", "--destination", temp)
        archive = Path(temp) / identity["archive_filename"]
        assert invoke("verify", str(archive)) == identity
        assert invoke("list", "--destination", temp) == [
            {"archive_filename": archive.name, "state": "verified"}
        ]
        with tarfile.open(archive) as content:
            assert set(content.getnames()) == {
                "postcardscene.sqlite3",
                "config.py",
                "session.key",
                "backup-manifest.json",
            }
            assert content.extractfile("session.key").read() == KEY.read_bytes()
            assert (
                content.extractfile("config.py").read()
                == Path("/etc/postcardscene/config.py").read_bytes()
            )
        for path in Path(temp).iterdir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    print("Managed web-UID manual backup create/verify/list passed.")


def denied(operation):
    try:
        operation()
    except PermissionError:
        return
    raise AssertionError("Unexpected cross-UID authority")


def child(action, user, *, hold=False):
    account = pwd.getpwnam(user)
    process = subprocess.Popen(
        [PYTHON, "-I", "/opt/postcardscene-smoke.py", "worker", action],
        user=account.pw_uid,
        group=account.pw_gid,
        extra_groups=[],
        umask=0o007,
        env={"PATH": "/usr/bin:/bin"},
        cwd="/",
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if hold:
        # The entire smoke also has a CI timeout; select bounds the ready handshake.
        import select

        assert select.select([process.stdout], [], [], 15)[0], (
            "Worker readiness timeout"
        )
        assert process.stdout.readline() == "ready\n", process.communicate(timeout=2)
        return process
    output = process.communicate(timeout=15)
    assert process.returncode == 0, output


def release_child(process):
    output = process.communicate("done\n", timeout=15)
    assert process.returncode == 0, output


def permissions(runtime, web, shared):
    assert runtime != web and 0 not in (runtime, web)
    for owner, peer, uid in (
        ("postcardscene", "postcardscene-web", runtime),
        ("postcardscene-web", "postcardscene", web),
    ):
        holder = child("hold", owner, hold=True)
        try:
            for suffix in ("-wal", "-shm"):
                info = (STATE / ("postcardscene.sqlite3" + suffix)).stat()
                assert info.st_uid == uid and info.st_gid == shared
                assert stat.S_IMODE(info.st_mode) == 0o660, oct(
                    stat.S_IMODE(info.st_mode)
                )
            child("write", peer)
        finally:
            release_child(holder)
    assert (STATE / "postcardscene.sqlite3").stat().st_uid == runtime
    assert KEY.stat().st_uid == web and stat.S_IMODE(KEY.stat().st_mode) == 0o600
    assert os.getgrouplist("postcardscene-web", shared) == [shared]
    child("runtime-private", "postcardscene")
    server = child("socket", "postcardscene", hold=True)
    try:
        assert stat.S_IMODE(SOCKET.stat().st_mode) == 0o660
        child("web-access", "postcardscene-web")
    finally:
        release_child(server)


def doctor_layout():
    from postcardscene.doctor import CHECKS
    from postcardscene.doctor.cli import collect
    from postcardscene.doctor.metadata import PACKAGE, inspect

    # Fixed installed payload metadata makes packaging/DAC failures reviewable.
    for label, path in (
        ("active", Path("/opt/postcardscene/venv")),
        ("venv", PACKAGE.parents[3]),
        ("package", PACKAGE),
        ("labwc_config", PACKAGE / "graphics/labwc/rc.xml"),
        ("labwc_autostart", PACKAGE / "graphics/labwc/autostart"),
    ):
        info = path.lstat()
        print(
            label,
            info.st_uid,
            info.st_gid,
            oct(stat.S_IMODE(info.st_mode)),
            info.st_nlink,
            flush=True,
        )
    ready = {
        "release",
        "config_authority",
        "installed_assets",
        "conflict_record",
        "identities",
        "durable_permissions",
        "database_permissions",
        "private_key_permissions",
        "cache_permissions",
        "runtime_permissions",
        "wayland_permissions",
        "database",
        "catalog",
        "web_config",
    }
    database = STATE / "postcardscene.sqlite3"
    before = database.read_bytes()
    key_info = KEY.stat()
    for _ in range(2):
        report = collect()
        assert tuple(c.identifier for c in report.checks) == CHECKS
        assert all(
            c.state == "ready" for c in report.checks if c.identifier in ready
        ), report
        assert database.read_bytes() == before
        assert KEY.stat() == key_info
    # Exact installed authorities fail individually; doctor never recreates them.
    for path, identifier in (
        (Path("/opt/postcardscene/venv"), "release"),
        (Path("/etc/postcardscene/config.py"), "config_authority"),
        (Path("/etc/systemd/system/postcardscene-runtime.service"), "installed_assets"),
        (Path("/etc/pam.d/postcardscene-graphics"), "installed_assets"),
        (
            Path("/etc/systemd/system/postcardscene-web.service.d/permissions.conf"),
            "installed_assets",
        ),
        (Path("/etc/tmpfiles.d/postcardscene.conf"), "installed_assets"),
        (PACKAGE / "graphics/labwc/autostart", "installed_assets"),
        (Path("/opt/postcardscene/service-conflicts.json"), "conflict_record"),
        (KEY, "private_key_permissions"),
        (database, "database_permissions"),
    ):
        saved = path.with_name(path.name + ".doctor-smoke")
        path.rename(saved)
        try:
            assert inspect(identifier).state in {"degraded", "fatal"}
            assert not path.exists()
        finally:
            saved.rename(path)
    print(
        "Installed doctor metadata, DB, redaction boundaries and repeat-run preservation passed."
    )


def bootstrap_runner(installer, args, **options):
    if not options.get("interactive"):
        return installer.command(args, **options)
    # Disposable test credentials travel on stdin only, never argv/env or output.
    account = pwd.getpwnam("postcardscene-web")
    password = secrets.token_urlsafe(32)
    result = subprocess.run(
        args,
        input=f"smoke-admin\n{password}\n{password}\n",
        text=True,
        user=account.pw_uid,
        group=account.pw_gid,
        extra_groups=[],
        umask=0o007,
        env={**installer.ENV, "POSTCARDSCENE_CONFIG": "/etc/postcardscene/config.py"},
        cwd="/",
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, "Administrator CLI bootstrap failed"


def service_smoke(installer, runtime, web):
    installer.command(("/usr/bin/systemctl", "daemon-reload"))
    for unit, uid in (
        ("postcardscene-runtime.service", runtime),
        ("postcardscene-web.service", web),
    ):
        installer.command(("/usr/bin/systemctl", "start", unit))
        result = subprocess.run(
            ["systemctl", "show", unit, "--property=MainPID", "--value"],
            capture_output=True,
            text=True,
            check=True,
        )
        pid = int(result.stdout.strip())
        assert Path(f"/proc/{pid}").stat().st_uid == uid
        result = subprocess.run(
            ["systemctl", "show", unit, "--property=UMask", "--value"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "0007"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        connection = HTTPConnection("127.0.0.1", 8080, timeout=1)
        try:
            connection.request("GET", "/login", headers={"Host": "localhost"})
            response = connection.getresponse()
            assert response.status == 200
            break
        except OSError:
            time.sleep(0.1)
        finally:
            connection.close()
    else:
        raise AssertionError("Installed web readiness timeout")
    installer.command(
        (
            "/usr/bin/systemctl",
            "is-active",
            "--quiet",
            "postcardscene-runtime.service",
            "postcardscene-web.service",
        )
    )
    installer.command(("/usr/bin/systemctl", "stop", *installer.SERVICES))
    print(
        "Canonical runtime/web units ran under distinct installed UIDs; graphics hardware unverified."
    )


def rejected_layouts(entrypoint, bundle):
    marker = Path("/opt/postcardscene/service-conflicts.json")
    saved = Path("/opt/postcardscene-marker-smoke")
    retained = marker.read_bytes()
    for kind in ("missing", "damaged", "symlink", "foreign", "mixed"):
        marker.rename(saved)
        try:
            if kind == "damaged":
                marker.write_bytes(b"{}")
            elif kind == "symlink":
                marker.symlink_to(saved)
            elif kind in ("foreign", "mixed"):
                marker.write_bytes(retained)
                if kind == "foreign":
                    os.chown(marker, pwd.getpwnam("postcardscene").pw_uid, 0)
                else:
                    Path("/opt/postcardscene/unowned").mkdir()
            try:
                entrypoint.Installation(bundle).remove()
            except entrypoint.InstallError:
                pass
            else:
                raise AssertionError("Unrecognized layout was removed")
            assert Path("/opt/postcardscene/venv").is_symlink()
            assert KEY.exists() and (STATE / "postcardscene.sqlite3").exists()
        finally:
            if os.path.lexists(marker):
                marker.unlink()
            saved.rename(marker)
            if kind == "mixed":
                Path("/opt/postcardscene/unowned").rmdir()


def failed_reinstalls(entrypoint, bundle, validated, before):
    version, _, wheel, _, preflight, host = validated
    compatibility = entrypoint.validate_preserved_application
    stage = host.stage_payload

    def reject(*args, **kwargs):
        raise entrypoint.InstallError("injected_pre_durable_failure")

    try:
        for failure in ("before_release", "partial_stage", "compatibility"):
            host.stage_payload = reject if failure == "before_release" else stage
            entrypoint.validate_preserved_application = (
                reject if failure == "compatibility" else compatibility
            )
            operation = entrypoint.Installation(
                bundle, reject if failure == "partial_stage" else host.command
            )
            try:
                operation.install()
            except entrypoint.InstallError as error:
                assert str(error) == "injected_pre_durable_failure"
            else:
                raise AssertionError("Injected reinstall failure was not reached")
            assert not operation.durable
            assert operation.phase == (
                "preserved_compatibility" if failure == "compatibility" else "payload"
            )
            operation.recover()
            assert not os.path.lexists(host.RELEASES)
            assert host.classify(version, wheel, preflight) == "removed_preserved"
            for path, expected in before.items():
                info = path.stat()
                assert (
                    path.read_bytes(),
                    info.st_uid,
                    info.st_gid,
                    info.st_mode,
                ) == expected
            print(f"Reinstall {failure}: recovered exact removed_preserved authority.")
    finally:
        host.stage_payload = stage
        entrypoint.validate_preserved_application = compatibility


def prepare_smoke_bundle(bundle, entrypoint):
    # The copied two-UID worker remains standalone outside the source checkout.
    from release_bundle import write_manifest

    shutil.copyfile(sys.argv[1], bundle / Path(sys.argv[1]).name)
    for name in ("install.py", *entrypoint.INPUT_HASHES):
        shutil.copyfile(ROOT / name, bundle / name)
    # Root in the disposable CI VM reads this explicitly selected runner checkout.
    sha = subprocess.check_output(
        ["git", "-c", f"safe.directory={ROOT}", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
    ).strip()
    write_manifest(bundle, Path(sys.argv[1]).name.split("-")[1], sha)


def lifecycle_smoke(entrypoint, installer, runtime, web, shared):
    # Runtime/web are real systemd services; graphics start alone is substituted
    # because this x86 VM has no supported seat/display. No ownership gate is bypassed.
    before = {
        p: (p.read_bytes(), p.stat().st_uid, p.stat().st_gid, p.stat().st_mode)
        for p in (
            STATE / "postcardscene.sqlite3",
            KEY,
            Path("/etc/postcardscene/config.py"),
        )
    }
    with tempfile.TemporaryDirectory() as scratch:
        bundle = Path(scratch)
        prepare_smoke_bundle(bundle, entrypoint)
        rejected_layouts(entrypoint, bundle)
        operation = entrypoint.Installation(bundle)
        try:
            operation.remove()
        except entrypoint.InstallError:
            # Disposable runner diagnostics omit process arguments and secrets.
            subprocess.run(
                ("ps", "-u", "postcardscene,postcardscene-web", "-o", "pid,uid,comm"),
                check=False,
            )
            _, support, observer, _ = entrypoint.load_support(bundle)
            for label, probe in (
                ("durable", support.preserved_authority),
                ("marker", lambda: support.conflict_record(observer)),
                (
                    "units",
                    lambda: observer.service_check(observer.Host(), None, "preserved"),
                ),
            ):
                try:
                    probe()
                    print(label, "valid", flush=True)
                except (support.InstallError, observer.Rejected) as error:
                    print(label, str(error), flush=True)
                    if label == "units":
                        for unit in (*support.SERVICES, *support.CONFLICTS):
                            print(
                                unit,
                                observer.Host().command(
                                    (
                                        "/usr/bin/systemctl",
                                        "show",
                                        unit,
                                        "--all",
                                        "--property=LoadState,ActiveState,UnitFileState",
                                    )
                                ),
                                flush=True,
                            )
            raise
        assert operation.phase == "removed_preserved"
        marker = Path("/opt/postcardscene/service-conflicts.json")
        retained = marker.read_bytes()
        operation.remove()
        assert operation.phase == "already_removed_preserved"
        assert marker.read_bytes() == retained
        # Deliberate operator change while removed must become the next restore target.
        installer.command(("/usr/bin/systemctl", "mask", "getty@tty1.service"))
        validated = entrypoint.validate_inputs(bundle)
        preflight, host = validated[-2:]
        plan = SimpleNamespace(python="/usr/bin/python3", tools=())
        preflight.preflight = lambda **kw: SimpleNamespace(ok=True, plan=plan)
        host.install_packages = lambda *args: None
        original = entrypoint.validate_inputs
        entrypoint.validate_inputs = lambda _: validated

        def run(args, **options):
            if args == (
                "/usr/bin/systemctl",
                "start",
                "postcardscene-graphics.service",
            ):
                host.directory(
                    Path("/run/postcardscene-wayland"), runtime, shared, 0o700
                )
                return
            if args[:3] == ("/usr/bin/systemctl", "is-active", "--quiet"):
                args = (
                    *args[:3],
                    *[u for u in args[3:] if u != "postcardscene-graphics.service"],
                )
            host.command(args, **options)

        try:
            failed_reinstalls(entrypoint, bundle, validated, before)
            reinstall = entrypoint.Installation(bundle, run)
            reinstall.install()
            assert reinstall.phase == "complete"
        finally:
            entrypoint.validate_inputs = original
        refreshed = json.loads(marker.read_bytes())
        assert refreshed["getty@tty1.service"]["LoadState"] == "masked"
        host.command(("/usr/bin/systemctl", "stop", *host.SERVICES))
        for path, expected in before.items():
            assert (
                path.read_bytes(),
                path.stat().st_uid,
                path.stat().st_gid,
                path.stat().st_mode,
            ) == expected
        assert host.preserved_identities()[:3] == (runtime, web, shared)
        if not Path("/run/postcardscene-wayland").exists():
            host.directory(Path("/run/postcardscene-wayland"), runtime, shared, 0o700)
        subprocess.run(
            (PYTHON, "-I", "-B", "/opt/postcardscene-smoke.py", "worker", "doctor"),
            check=True,
            timeout=60,
            env=host.ENV,
        )
        permissions(runtime, web, shared)
        entrypoint.Installation(bundle).remove()
        assert (
            host.service_state(preflight, "getty@tty1.service")["LoadState"] == "masked"
        )
    print(
        "Real remove/reinstall preserved DB/admin/config/key bytes and stable UIDs; refreshed conflict restoration passed."
    )


def main():
    assert os.geteuid() == 0, "Run only as root in a disposable Linux CI VM"
    assert Path("/proc/1/comm").read_text().strip() == "systemd"
    spec = importlib.util.spec_from_file_location("native_install", ROOT / "install.py")
    entrypoint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entrypoint)
    with tempfile.TemporaryDirectory() as scratch:
        bundle = Path(scratch)
        prepare_smoke_bundle(bundle, entrypoint)
        version, wheel_name, wheel, requirements, preflight, installer = (
            entrypoint.validate_inputs(bundle)
        )
    # Refuse any existing installation; this smoke has no adoption or cleanup path.
    for path in (
        *installer.ASSETS.values(),
        "/opt/postcardscene",
        "/etc/postcardscene",
        str(STATE),
        str(KEY.parent),
        "/var/cache/postcardscene",
        "/run/postcardscene",
        "/run/postcardscene-wayland",
        str(DEVICE),
    ):
        assert not os.path.lexists(path), "Disposable clean VM required"
    installer.directory(Path("/opt/postcardscene"))
    installer.directory(Path("/opt/postcardscene/releases"))
    release = Path("/opt/postcardscene/releases") / version
    python = installer.stage_payload(
        release,
        wheel_name,
        wheel,
        requirements,
        SimpleNamespace(python="/usr/bin/python3"),
        installer.command,
    )
    runtime, web, shared, private = installer.provision_identities(installer.command)
    installer.directory(Path("/etc/postcardscene"), gid=shared, mode=0o750)
    installer.write_new(
        Path("/etc/postcardscene/config.py"), installer.CONFIG, 0o640, shared
    )
    installer.directory(STATE, runtime, shared, 0o2770)
    installer.directory(KEY.parent, web, private, 0o700)
    installer.directory(Path("/var/cache/postcardscene"), runtime, shared, 0o700)
    installer.install_assets(wheel, installer.command)
    installer.directory(Path("/run/postcardscene-wayland"), runtime, shared, 0o700)
    os.mknod(DEVICE, stat.S_IFCHR | 0o660, os.makedev(1, 3))
    DEVICE.chmod(0o660)
    installer.bootstrap(
        python, lambda args, **kw: bootstrap_runner(installer, args, **kw)
    )
    Path("/opt/postcardscene/venv").symlink_to(release / "venv")
    installer.write_new(
        Path("/opt/postcardscene-smoke.py"), Path(__file__).read_bytes()
    )
    permissions(runtime, web, shared)
    installer.reserve_graphics(preflight, installer.command)
    subprocess.run(
        (PYTHON, "-I", "-B", "/opt/postcardscene-smoke.py", "worker", "doctor"),
        check=True,
        timeout=60,
        env=installer.ENV,
    )
    service_smoke(installer, runtime, web)
    child("backup", "postcardscene-web")
    from smoke_scheduled_backup import scheduled_lifecycle

    scheduled_lifecycle(
        entrypoint,
        installer,
        preflight,
        lambda: lifecycle_smoke(entrypoint, installer, runtime, web, shared),
    )
    print(
        "Real two-UID SQLite/WAL/SHM, private key, panel socket and device/Wayland DAC passed."
    )


if __name__ == "__main__":
    if sys.argv[1] == "worker":
        worker(sys.argv[2])
    else:
        main()
