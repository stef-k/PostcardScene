"""One preserved recovery pair across disposable installed-Linux host loss.

Only called after smoke_native_install has refused all pre-existing product roots.
No production teardown API is added. Generic graphics activation stays simulated.
"""

import grp
import json
import os
import pwd
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace

from smoke_restore import DATABASE, KEY, PYTHON, restore_archive
from smoke_scheduled_backup import scheduled_lifecycle, web_python

CONFIG = Path("/etc/postcardscene/config.py")
STATE_HELPER = Path(__file__).with_name("smoke_recovery_state.py").read_text()


def state(action):
    return web_python(
        'import sys; code = sys.argv[2]; sys.argv = ["evidence", sys.argv[1]]; '
        + 'exec(compile(code, "recovery-evidence", "exec"))',
        action,
        STATE_HELPER,
    )


def backup_cli(*args):
    return json.loads(
        web_python(
            "import sys; from postcardscene.backup.cli import main; "
            "raise SystemExit(main(sys.argv[1:]))",
            *args,
        )
    )


def verify(archive):
    # The actual frozen dataclass crosses only a private serialization boundary;
    # all fields, not just its hash, are compared on every recheck.
    return json.loads(
        root_python(
            "import json, sys; from dataclasses import asdict, FrozenInstanceError; "
            "from postcardscene.backup import verify; v = verify(sys.argv[1]); "
            '\ntry:\n v.schema_revision = "changed"\n'
            "except FrozenInstanceError:\n pass\n"
            'else:\n raise AssertionError("Mutable verification identity")\n'
            "print(json.dumps(asdict(v), sort_keys=True))",
            str(archive),
        )
    )


def root_python(code, *args):
    result = subprocess.run(
        (PYTHON, "-I", "-B", "-c", code, *args), capture_output=True, timeout=60
    )
    assert result.returncode == 0, "Installed root recovery evidence failed"
    return result.stdout


def authority(host, preflight, version, wheel):
    host.installed_authority(version, wheel)
    host.service_authority(preflight)
    host.require_auxiliary_installed(preflight)
    root_python(
        "from postcardscene.doctor.cli import collect; "
        'r = collect(); required = {"release", "config_authority", '
        '"installed_assets", "conflict_record", "identities", "database", '
        '"database_permissions", "private_key_permissions", "web_config"}; '
        'assert all(c.state == "ready" for c in r.checks if c.identifier in required); '
        'b = next(c for c in r.checks if c.identifier == "backup"); '
        'assert b.state != "not_implemented" and not b.details'
    )


def lost_host(entrypoint, bundle, host, preflight, version, wheel):
    # Normal removal first retires units, validates/deletes exact packaged assets,
    # discards the release and restores conflicts. Only disposable durable roots
    # remain for this test-only host-loss step.
    entrypoint.Installation(bundle).remove()
    assert host.classify(version, wheel, preflight) == "removed_preserved"
    for root in (
        Path("/opt/postcardscene"),
        Path("/etc/postcardscene"),
        Path("/var/lib/postcardscene"),
        Path("/var/lib/postcardscene-web"),
    ):
        assert root.is_dir() and not root.is_symlink()
        shutil.rmtree(root)
    for account in ("postcardscene-web", "postcardscene"):
        host.command(("/usr/sbin/userdel", account))
    # userdel may remove the now-unused same-named private group itself.
    for group in ("postcardscene-web", "postcardscene"):
        if any(g.gr_name == group for g in grp.getgrall()):
            host.command(("/usr/sbin/groupdel", group))
    host.command(("/usr/bin/systemctl", "daemon-reload"))
    assert host.classify(version, wheel, preflight) == "clean"
    preflight.storage_check(preflight.Host())
    preflight.service_check(preflight.Host(), None)


def clean_install(entrypoint, bundle):
    from smoke_native_install import bootstrap_runner

    validated = entrypoint.validate_inputs(bundle)
    preflight, host = validated[-2:]
    original = entrypoint.validate_inputs
    original_stdin = entrypoint.sys.stdin

    # Keep real clean storage/identity/unit preflight. Only ARM64/package facts
    # and interactive credential transport are substituted on the generic VM.
    def preflight_check(**kwargs):
        preflight.storage_check(preflight.Host())
        preflight.service_check(preflight.Host(), None)
        return SimpleNamespace(
            ok=True, plan=SimpleNamespace(python="/usr/bin/python3", tools=())
        )

    preflight.preflight = preflight_check
    host.install_packages = lambda *args: None

    def run(args, **options):
        if args == ("/usr/bin/systemctl", "start", "postcardscene-graphics.service"):
            runtime = pwd.getpwnam("postcardscene")
            host.directory(
                Path("/run/postcardscene-wayland"),
                runtime.pw_uid,
                runtime.pw_gid,
                0o700,
            )
            return
        if args[:3] == ("/usr/bin/systemctl", "is-active", "--quiet"):
            args = (
                *args[:3],
                *[u for u in args[3:] if u != "postcardscene-graphics.service"],
            )
        return bootstrap_runner(host, args, **options)

    try:
        entrypoint.validate_inputs = lambda _: validated
        entrypoint.sys.stdin = SimpleNamespace(isatty=lambda: True)
        operation = entrypoint.Installation(bundle, run)
        operation.install()
        assert operation.phase == "complete"
    finally:
        entrypoint.validate_inputs = original
        entrypoint.sys.stdin = original_stdin
    return validated


def recovery_smoke(entrypoint, host, preflight):
    from smoke_native_install import lifecycle_smoke, prepare_smoke_bundle

    with tempfile.TemporaryDirectory(
        prefix="postcardscene-recovery-", dir="/tmp"
    ) as temp:
        root = Path(temp)
        # The root-private bundle and web-private backup destination survive every
        # product teardown; scheduled retention uses a separate TemporaryDirectory.
        root.chmod(0o711)
        bundle = root / "release"
        bundle.mkdir(mode=0o700)
        prepare_smoke_bundle(bundle, entrypoint)
        version, _, wheel, _, _, _ = entrypoint.validate_inputs(bundle)
        inputs = {p.name: p.read_bytes() for p in bundle.iterdir()}
        destination = root / "recovery"
        destination.mkdir(mode=0o700)
        web = pwd.getpwnam("postcardscene-web")
        os.chown(destination, web.pw_uid, web.pw_gid)
        CONFIG.write_bytes(CONFIG.read_bytes() + b"# Original recovery configuration\n")
        expected = state("seed")
        exact = {p: p.read_bytes() for p in (CONFIG, KEY)}
        created = backup_cli("create", "--destination", str(destination))
        archive = destination / created["archive_filename"]
        identity = verify(archive)
        assert identity == created
        assert backup_cli("list", "--destination", str(destination)) == [
            {"archive_filename": archive.name, "state": "verified"}
        ]
        pair = {p.name: p.read_bytes() for p in destination.iterdir()}
        assert len(pair) == 2
        assert all(
            stat.S_IMODE(p.stat().st_mode) == 0o600 for p in destination.iterdir()
        )
        with tarfile.open(archive) as content:
            assert set(content.getnames()) == {
                "postcardscene.sqlite3",
                "config.py",
                "session.key",
                "backup-manifest.json",
            }
            assert content.extractfile("config.py").read() == exact[CONFIG]
            assert content.extractfile("session.key").read() == exact[KEY]
        immutable = {p: p.read_bytes() for p in host.asset_bytes(wheel)}
        marker = Path("/opt/postcardscene/service-conflicts.json")
        immutable[marker] = marker.read_bytes()
        for path in (DATABASE, CONFIG, KEY):
            path.write_bytes(b"distinct damaged current content")
        restore_archive(archive)
        assert state("restored") == expected
        assert all(
            p.read_bytes() == value for p, value in {**exact, **immutable}.items()
        )
        authority(host, preflight, version, wheel)
        host.command(("/usr/bin/systemctl", "stop", *host.SERVICES))
        runtime, web, shared, _ = host.preserved_identities()
        scheduled_lifecycle(
            entrypoint,
            host,
            preflight,
            lambda: lifecycle_smoke(entrypoint, host, runtime, web, shared),
        )
        assert verify(archive) == identity
        assert pair == {p.name: p.read_bytes() for p in destination.iterdir()}
        lost_host(entrypoint, bundle, host, preflight, version, wheel)
        assert inputs == {p.name: p.read_bytes() for p in bundle.iterdir()}
        validated = clean_install(entrypoint, bundle)
        fresh_host = validated[-1]
        fresh_preflight = validated[-2]
        assert KEY.read_bytes() != exact[KEY] and CONFIG.read_bytes() != exact[CONFIG]
        fresh = json.loads(state("fresh"))
        assert fresh["administrator"] != json.loads(expected)["administrator"]
        assert all(
            not fresh[t]
            for t in ("source", "widget", "scene", "sequence", "operating_window")
        )
        assert fresh["backup_policy"] != json.loads(expected)["backup_policy"]
        assert (
            fresh["application_settings"]
            != json.loads(expected)["application_settings"]
        )
        fresh_marker = marker.read_bytes()
        # Replacement UIDs need not match the lost host. Root restore can read the
        # untouched pair; strict API rechecks also run as root from this point.
        restore_archive(archive)
        assert state("restored") == expected
        assert all(p.read_bytes() == value for p, value in exact.items())
        assert marker.read_bytes() == fresh_marker
        assert all(
            p.read_bytes() == value for p, value in immutable.items() if p != marker
        )
        authority(fresh_host, fresh_preflight, version, wheel)
        assert verify(archive) == identity
        assert pair == {p.name: p.read_bytes() for p in destination.iterdir()}
    print(
        "Same canonical backup recovered original and clean replacement hosts; graphics simulated."
    )
