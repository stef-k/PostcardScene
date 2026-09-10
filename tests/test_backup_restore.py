"""Non-destructive restore intake with real root DAC in disposable Linux CI."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import pytest
from test_backup import built as built
from test_backup import progress, rewrite, sidecar
from test_backup_retention import older_pair as older_pair

from postcardscene import backup
from postcardscene.accounts import Administrator
from postcardscene.backup import archive, restore, retention
from postcardscene.backup.files import DB_NAME, MANIFEST_NAME, BackupError
from postcardscene.persistence import Database


@pytest.fixture
def staging():
    if os.geteuid() != 0:
        pytest.skip("Real root staging authority runs in the privileged CI step")
    root = Path(tempfile.mkdtemp(prefix="postcardscene-restore-test-", dir="/tmp"))
    try:
        yield root
    finally:
        if root.is_symlink():
            root.unlink()
        elif root.exists():
            shutil.rmtree(root)


@pytest.fixture
def restorable(staging, built):
    scratch, path, _ = built
    database = Database(scratch / DB_NAME)
    try:
        with database.transaction() as session:
            session.add(
                Administrator(
                    id=1,
                    username="private-admin",
                    password_hash="private-account-hash",
                    session_id="private-session-identity",
                )
            )
    finally:
        database.engine.dispose()
    (scratch / path.name).unlink()
    source = archive.build(
        scratch,
        datetime(2026, 9, 10, tzinfo=timezone.utc),
        version("postcardscene"),
        progress,
    )
    path.write_bytes(source.read_bytes())
    sidecar(path)
    return path


def replace_member(path, name, value):
    def change(members):
        result = []
        for info, data in members:
            if info.name == name:
                data = value
            if info.name == MANIFEST_NAME:
                manifest = json.loads(data)
                manifest["members"][name] = {
                    "size": len(value),
                    "sha256": hashlib.sha256(value).hexdigest(),
                }
                data = json.dumps(manifest).encode()
            result.append((info, data))
        return result

    rewrite(path, change)


def test_root_required_before_worker_or_staging(tmp_path, monkeypatch):
    before = set(tmp_path.iterdir())
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(BackupError, match="^restore_root_required$"):
        restore.prepare_restore(tmp_path / "absent", tmp_path / "absent-stage")
    assert set(tmp_path.iterdir()) == before


def test_isolated_candidate_survives_destination_removal(
    restorable, staging, tmp_path, monkeypatch
):
    original = subprocess.Popen
    calls = []

    def observe(args, **kwargs):
        calls.append((args, kwargs))
        return original(args, **kwargs)

    monkeypatch.setattr(backup.subprocess, "Popen", observe)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "absent"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "absent"))
    monkeypatch.chdir(tmp_path)
    before = {p.name: p.read_bytes() for p in restorable.parent.iterdir()}
    candidate = restore.prepare_restore(restorable, staging)
    assert before == {p.name: p.read_bytes() for p in restorable.parent.iterdir()}
    assert len(calls) == 1
    args, options = calls[0]
    assert args[1:5] == ["-I", "-B", "-m", "postcardscene.backup.worker"]
    assert args[5] == "restore-stage" and options["cwd"] == "/"
    assert candidate.verified == backup.verify(restorable)
    assert len(candidate.members) == 5
    for secret in (
        str(staging),
        "private-admin",
        "private-account-hash",
        "private.example",
    ):
        assert secret not in repr(candidate)
    with pytest.raises(FrozenInstanceError):
        candidate.staging_root = tmp_path
    shutil.rmtree(restorable.parent)
    restore.revalidate_restore(candidate)
    assert {p.name for p in staging.iterdir()} == {m.name for m in candidate.members}


@pytest.mark.parametrize(
    "damage",
    [
        "checksum",
        "archive",
        "member",
        "manifest",
        "database",
        "schema",
        "application_id",
    ],
)
def test_corruption_rejects_and_cleans_local_candidate(restorable, staging, damage):
    if damage == "checksum":
        restorable.with_name(restorable.name + ".sha256").unlink()
    elif damage == "archive":
        with restorable.open("ab") as stream:
            stream.write(b"corruption")
    elif damage == "database":
        replace_member(restorable, DB_NAME, b"not a database")
    else:

        def change(members):
            result = []
            for info, value in members:
                if damage == "member" and info.name == "config.py":
                    value = b"damaged"
                if info.name == MANIFEST_NAME:
                    manifest = json.loads(value)
                    if damage == "manifest":
                        manifest["extra"] = "private-detail"
                    elif damage == "schema":
                        manifest["schema_revision"] = "9999_future"
                    elif damage == "application_id":
                        manifest["sqlite_application_id"] = 0
                    value = json.dumps(manifest).encode()
                result.append((info, value))
            return result

        rewrite(restorable, change)
    with pytest.raises(BackupError, match="^restore_preparation_failed$"):
        restore.prepare_restore(restorable, staging)
    assert not staging.exists()


@pytest.mark.parametrize("other_version", ["0.0.1", "9.0.0"])
def test_other_application_versions_reject(staging, built, other_version):
    scratch, path, _ = built
    source = archive.build(
        scratch, datetime(2026, 9, 11, tzinfo=timezone.utc), other_version, progress
    )
    path = path.parent / source.name
    path.write_bytes(source.read_bytes())
    sidecar(path)
    assert backup.verify(path).application_version == other_version
    with pytest.raises(BackupError, match="^restore_preparation_failed$"):
        restore.prepare_restore(path, staging)


def test_retention_owned_old_schema_is_not_restore_authority(
    staging, built, older_pair
):
    _, current, verified = built
    assert retention.retain(
        current.parent, 2, current.name, verified.archive_sha256, progress
    ) == {"result": "ready"}
    assert older_pair.exists()
    with pytest.raises(BackupError, match="^restore_preparation_failed$"):
        restore.prepare_restore(older_pair, staging)


@pytest.mark.parametrize(
    "config",
    [
        b"DATABASE_PATH = '/redirected'",
        b"SESSION_SECRET_PATH = '/redirected'",
        b"open('/tmp/postcardscene-must-not-execute', 'w').write('secret')",
    ],
)
def test_archived_config_is_canonical_and_never_executed(restorable, staging, config):
    replace_member(restorable, "config.py", config)
    with pytest.raises(BackupError, match="^restore_preparation_failed$"):
        restore.prepare_restore(restorable, staging)
    assert not Path("/tmp/postcardscene-must-not-execute").exists()


def test_missing_administrator_rejects(staging, built):
    with pytest.raises(BackupError, match="^restore_preparation_failed$"):
        restore.prepare_restore(built[1], staging)


@pytest.mark.parametrize("size", [31, 33])
def test_wrong_key_size_rejects(restorable, staging, size):
    replace_member(restorable, "session.key", b"x" * size)
    with pytest.raises(BackupError, match="^restore_preparation_failed$"):
        restore.prepare_restore(restorable, staging)


@pytest.mark.parametrize(
    "damage", ["symlink", "mode", "owner", "nonempty", "outside_tmp"]
)
def test_unsafe_staging_is_rejected_without_cleanup_of_unowned_files(
    restorable, staging, tmp_path, damage
):
    root = staging
    if damage == "symlink":
        staging.rmdir()
        staging.symlink_to(tmp_path, target_is_directory=True)
    elif damage == "mode":
        staging.chmod(0o750)
    elif damage == "owner":
        os.chown(staging, 12345, 12345)
    elif damage == "nonempty":
        (staging / "foreign").write_text("preserve")
    else:
        root = tmp_path
    with pytest.raises(BackupError, match="^restore_preparation_failed$"):
        restore.prepare_restore(restorable, root)
    assert root.exists()
    if damage == "nonempty":
        assert (staging / "foreign").read_text() == "preserve"


@pytest.mark.parametrize(
    "damage", ["bytes", "symlink", "hardlink", "mode", "owner", "root", "extra"]
)
def test_local_tampering_rejects(restorable, staging, tmp_path, damage):
    candidate = restore.prepare_restore(restorable, staging)
    target = staging / "config.py"
    if damage == "bytes":
        target.write_bytes(b"different config")
    elif damage == "symlink":
        target.unlink()
        target.symlink_to(tmp_path / "absent")
    elif damage == "hardlink":
        os.link(target, tmp_path / "linked")
    elif damage == "mode":
        target.chmod(0o640)
    elif damage == "owner":
        os.chown(target, 12345, 12345)
    elif damage == "root":
        shutil.rmtree(staging)
        staging.mkdir(mode=0o700)
    else:
        (staging / "unexpected").write_bytes(b"extra")
    with pytest.raises(BackupError, match="^restore_candidate_invalid$"):
        restore.revalidate_restore(candidate)


def test_stalled_restore_worker_is_bounded_and_local_scratch_cleaned(
    staging, monkeypatch
):
    original = subprocess.Popen
    children = []

    def stalled(args, **options):
        child = original([args[0], "-c", "import time; time.sleep(60)"], **options)
        children.append(child)
        return child

    monkeypatch.setattr(backup.subprocess, "Popen", stalled)
    started = time.monotonic()
    with pytest.raises(BackupError, match="^operation_timeout_or_cancelled$"):
        restore.prepare_restore("/unavailable/archive", staging, idle_timeout=0.1)
    assert time.monotonic() - started < 3
    assert children[0].poll() is not None
    assert not staging.exists()


def test_uncertain_scratch_cleanup_is_redacted(staging, built, monkeypatch):
    def fail(*args):
        raise OSError("private-path-and-secret")

    monkeypatch.setattr(restore, "cleanup_staging", fail)
    with pytest.raises(BackupError, match="^restore_cleanup_uncertain$"):
        restore.prepare_restore(built[1], staging)


def test_worker_rejects_substituted_staging_identity(restorable, staging):
    info = staging.stat()
    with pytest.raises(BackupError, match="^backup_failed$"):
        backup._run(
            "restore-stage",
            restorable,
            arguments=(str(staging), str(info.st_dev), str(info.st_ino + 1)),
        )
    assert list(staging.iterdir()) == []


def test_parent_rejects_change_after_worker_verification(
    restorable, staging, monkeypatch
):
    original = backup._run

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        (staging / "config.py").write_bytes(b"changed after worker verification")
        return result

    monkeypatch.setattr(backup, "_run", changed)
    with pytest.raises(BackupError, match="^restore_preparation_failed$"):
        restore.prepare_restore(restorable, staging)
    assert not staging.exists()
