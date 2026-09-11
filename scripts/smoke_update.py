"""Single disposable installed-Linux lane: predecessor -> public target updater."""

import hashlib
import importlib.util
import json
import os
import pwd
import signal
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from release_bundle import extract_archive, verify_checksums
from smoke_recovery import CONFIG, KEY, clean_install, lost_host, state
from smoke_update_migration import ancestor_migration

PHASE = Path("/opt/postcardscene/update-state.json")
ACTIVE = Path("/opt/postcardscene/venv")
RELEASES = ACTIVE.parent / "releases"


def extracted(artifacts, destination):
    wheel = next(artifacts.glob("*.whl"))
    version = wheel.name.split("-")[1]
    archive = f"postcardscene-{version}-linux-native.tar.gz"
    # External checksum precedes any extracted installer code execution.
    verify_checksums(artifacts, (archive, wheel.name))
    if version == "0.0.0":
        # The frozen baseline builder already checked its own fixed member set.
        # It predates update helpers, so do not impose the candidate member set.
        destination.mkdir(mode=0o700)
        with tarfile.open(artifacts / archive) as contents:
            contents.extractall(destination, filter="data")
    else:
        extract_archive(artifacts / archive, destination, version)
    manifest = json.loads((destination / "release-manifest.json").read_text())
    print(
        json.dumps(
            {
                "version": version,
                "source_commit": manifest["source_commit"],
                "schema": manifest["schema"],
                "archive_sha256": hashlib.sha256(
                    (artifacts / archive).read_bytes()
                ).hexdigest(),
            }
        ),
        flush=True,
    )
    spec = importlib.util.spec_from_file_location(
        "evidence_installer", destination / "install.py"
    )
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    return entry, manifest


def invoke(bundle, destination, boundary="complete"):
    result = subprocess.run(
        (
            "/usr/bin/python3",
            "-I",
            "-B",
            str(Path(__file__).with_name("smoke_update_command.py")),
            str(bundle),
            str(destination),
            boundary,
        ),
        timeout=600,
    )
    assert result.returncode == (0 if boundary == "complete" else -signal.SIGKILL), (
        boundary
    )


def stopped(host, preflight):
    for unit in (*host.SERVICES, host.AUXILIARY_UNITS[1]):
        values = host.service_state(preflight, unit)
        assert values["ActiveState"] == "inactive", (unit, values)
        assert values["UnitFileState"] == "disabled", (unit, values)
    host.require_no_processes(host.preserved_identities())


def update_drill(
    source_entry, source, target_entry, target, destination, *, interrupted
):
    validated = clean_install(source_entry, source)
    version, _, wheel, _, preflight, host = validated
    assert version == "0.0.0"
    web = pwd.getpwnam("postcardscene-web")
    destination.mkdir(mode=0o700)
    os.chown(destination, web.pw_uid, web.pw_gid)
    expected = state("seed")
    preserved = {p: p.read_bytes() for p in (CONFIG, KEY, host.MARKER)}
    identities = host.preserved_identities()
    old_assets = {p: p.read_bytes() for p in host.asset_bytes(wheel)}
    assert host.classify(version, wheel, preflight) == "installed_managed"
    if interrupted:
        invoke(target, destination, "staging")
        assert not PHASE.exists() and ACTIVE.readlink() == RELEASES / version / "venv"
        residue = next(p for p in RELEASES.iterdir() if p.name != version)
        assert not list(residue.iterdir())
        assert state("snapshot") == expected
        assert all(p.read_bytes() == b for p, b in old_assets.items())
        invoke(target, destination, "prepared")
        assert json.loads(PHASE.read_text())["phase"] == "prepared"
        # Persisting prepared did not stop the old runtime/web/timer.
        for unit in (*host.SERVICES[1:], host.AUXILIARY_UNITS[1]):
            assert host.service_state(preflight, unit)["ActiveState"] == "active"
        invoke(target, destination, "migrating")
        assert json.loads(PHASE.read_text())["phase"] == "migrating"
        stopped(host, preflight)
        assert ACTIVE.readlink() == RELEASES / version / "venv"
        assert all(p.read_bytes() == b for p, b in old_assets.items())
        invoke(target, destination, "committed")
        assert json.loads(PHASE.read_text())["phase"] == "committed"
        stopped(host, preflight)
        assert ACTIVE.readlink() == RELEASES / version / "venv"
        assert ACTIVE.with_name("venv.postcardscene-update.new").is_symlink()
    pairs = {p: p.read_bytes() for p in destination.iterdir()}
    invoke(target, destination)
    assert state("snapshot") == expected
    assert all(p.read_bytes() == data for p, data in preserved.items())
    assert all(p.read_bytes() == data for p, data in pairs.items())
    assert len(list(destination.glob("*.tar.gz"))) >= 1
    version, _, wheel, _, preflight, host = target_entry.validate_inputs(target)
    assert host.classify(version, wheel, preflight) == "installed_managed"
    assert host.preserved_identities() == identities
    assert (
        not PHASE.exists()
        and not PHASE.with_name(PHASE.name + ".postcardscene-update.new").exists()
    )
    assert list(RELEASES.iterdir()) == [RELEASES / version]
    for path in (*host.asset_bytes(wheel), ACTIVE):
        assert not os.path.lexists(
            path.with_name(path.name + ".postcardscene-update.new")
        )
    print(
        f"Public update complete; interruptions={interrupted}; durable state/config/key preserved; target installed_managed",
        flush=True,
    )
    return version, wheel, preflight, host


def main():
    assert os.geteuid() == 0 and Path("/proc/1/comm").read_text().strip() == "systemd"
    artifacts = Path(sys.argv[1])
    print((artifacts / "provenance.json").read_text(), flush=True)
    with tempfile.TemporaryDirectory(
        prefix="postcardscene-update-smoke-", dir="/tmp"
    ) as temp:
        root = Path(temp)
        root.chmod(0o755)
        source, target = root / "source", root / "target"
        source_entry, predecessor = extracted(artifacts / "predecessor", source)
        target_entry, candidate = extracted(artifacts / "candidate", target)
        assert predecessor["schema"] == candidate["schema"], (
            "First-release same-schema fixture changed"
        )
        version, _, wheel, _, preflight, host = target_entry.validate_inputs(target)
        # Existing install/restore smoke established ownership of this disposable host.
        # Normal removal precedes the existing test-only lost-host reset.
        for interrupted in (False, True):
            lost_host(target_entry, target, host, preflight, version, wheel)
            version, wheel, preflight, host = update_drill(
                source_entry,
                source,
                target_entry,
                target,
                root / f"backups-{interrupted}",
                interrupted=interrupted,
            )
        ancestor_migration(target_entry, target)
    print(
        "Installed Linux software authority only; no ARM64/Pi/HDMI/4K/acceleration/audio proof."
    )


if __name__ == "__main__":
    main()
