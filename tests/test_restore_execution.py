"""Real root file transaction, with systemd isolated at the fixed host seam."""

import os
import signal
import stat
from pathlib import Path

import pytest
from test_backup_restore import (
    built as built,
)
from test_backup_restore import (
    restorable as restorable,
)
from test_backup_restore import (
    staging as staging,
)

from postcardscene.backup import capture, operation, restore
from postcardscene.backup import restore_execute as engine
from postcardscene.backup import restore_files as files
from postcardscene.backup import restore_host as host
from postcardscene.backup.files import BackupError
from postcardscene.persistence import Database


@pytest.fixture
def managed(tmp_path, monkeypatch, restorable, staging):
    ids = (12001, 12002, 12001, 12002)
    paths = [tmp_path / name for name in ("config", "state", "web")]
    for path, uid, gid, mode in zip(
        paths,
        (0, ids[0], ids[1]),
        (ids[2], ids[2], ids[3]),
        (0o750, 0o2770, 0o700),
        strict=True,
    ):
        path.mkdir()
        os.chown(path, uid, gid)
        path.chmod(mode)
    for module in (capture,):
        monkeypatch.setattr(module, "CONFIG", paths[0] / "config.py")
        monkeypatch.setattr(module, "DATABASE", paths[1] / "postcardscene.sqlite3")
        monkeypatch.setattr(module, "KEY", paths[2] / "session.key")
    monkeypatch.setattr(operation, "KEY", capture.KEY)
    monkeypatch.setattr(operation, "identities", lambda: ids)
    lock = paths[2] / "backup.lock"
    lock.touch(mode=0o600)
    os.chown(lock, ids[1], ids[2])
    for entry in files.targets(ids):
        path, metadata, _, _ = entry
        path.write_bytes(b"corrupt old " + path.name.encode())
        os.chown(path, *metadata[:2])
        path.chmod(metadata[2])
        os.utime(path, ns=(123456789000, 987654321000))
    for suffix in ("-wal", "-shm"):
        path = capture.DATABASE.with_name(capture.DATABASE.name + suffix)
        path.write_bytes(b"old sidecar " + suffix.encode())
        os.chown(path, ids[1], ids[2])
        path.chmod(0o660)
    monkeypatch.setattr(host, "validate", lambda: (ids, "immutable"))
    monkeypatch.setattr(host, "immutable_authority", lambda: "immutable")
    calls = []
    monkeypatch.setattr(host, "stop", lambda identities: calls.append("stop"))

    def services():
        calls.append("services")
        with pytest.raises(BackupError, match="operation_busy"):
            with operation.restore_lock():
                pass

    monkeypatch.setattr(host, "activate_services", services)

    def timer():
        calls.append("timer")
        with operation.restore_lock():
            pass

    monkeypatch.setattr(host, "activate_timer", timer)
    # Archived default paths are canonical regardless of test filesystem routing.
    monkeypatch.setattr(capture, "validate_config", lambda data: None)
    candidate = restore.prepare_restore(restorable, staging)
    return ids, candidate, calls


def before(ids):
    return tuple((entry, files.current(*entry)) for entry in files.file_set(ids))


def test_corrupt_current_restored_without_destination(managed, restorable):
    ids, candidate, calls = managed
    restorable.unlink()
    engine.execute(candidate)
    assert calls == ["stop", "services", "timer"]
    database = Database(capture.DATABASE)
    database.check()
    database.engine.dispose()
    assert (
        capture.KEY.read_bytes()
        == (candidate.staging_root / "session.key").read_bytes()
    )
    assert len(files.file_set(ids)) == 3


@pytest.mark.parametrize("boundary", [1, 2, 3])
@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_replacement_failure_rolls_back_exact_bytes_metadata(
    managed, monkeypatch, boundary, failure
):
    ids, candidate, calls = managed
    original = before(ids)
    replace = files.replace
    count = 0

    def fail(*args, **kwargs):
        nonlocal count
        replace(*args, **kwargs)
        count += 1
        if count == boundary:
            raise failure("private error")

    monkeypatch.setattr(files, "replace", fail)
    with pytest.raises(BackupError, match="^restore_failed_rolled_back$"):
        engine.execute(candidate)
    assert before(ids) == original
    assert calls == ["stop", "stop"]


@pytest.mark.parametrize("phase", ["activate_services", "activate_timer"])
def test_postcommit_failure_keeps_recovered_data(managed, monkeypatch, phase):
    ids, candidate, calls = managed
    old = capture.DATABASE.read_bytes()

    def fail():
        raise OSError("private service output")

    monkeypatch.setattr(host, phase, fail)
    with pytest.raises(BackupError, match="^restore_activation_failed$"):
        engine.execute(candidate)
    assert capture.DATABASE.read_bytes() != old
    Database(capture.DATABASE).check()
    assert calls[-1] == "stop"


def test_rollback_failure_preserves_private_scratch(managed, monkeypatch):
    ids, candidate, calls = managed

    def fail(*args, **kwargs):
        raise OSError("private")

    monkeypatch.setattr(engine, "validate_data", fail)
    monkeypatch.setattr(files, "rollback", fail)
    roots = set(Path("/tmp").glob("postcardscene-rollback-*"))
    with pytest.raises(BackupError, match="^restore_manual_recovery_required$"):
        engine.execute(candidate)
    remaining = set(Path("/tmp").glob("postcardscene-rollback-*")) - roots
    assert len(remaining) == 1
    root = remaining.pop()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in root.iterdir())
    info = root.stat()
    restore.cleanup_staging(root, (info.st_dev, info.st_ino))
    assert calls == ["stop", "stop"]


def test_candidate_tamper_after_capture_is_rejected(managed, monkeypatch):
    ids, candidate, calls = managed
    original = before(ids)
    capture_current = files.capture_current

    def tamper(*args):
        result = capture_current(*args)
        (candidate.staging_root / "config.py").write_bytes(b"changed")
        return result

    monkeypatch.setattr(files, "capture_current", tamper)
    with pytest.raises(BackupError, match="^restore_failed_stopped$"):
        engine.execute(candidate)
    assert before(ids) == original
    assert calls == ["stop", "stop"]


@pytest.mark.parametrize("first", ["web", "root"])
def test_shared_inode_contention_both_directions(managed, monkeypatch, first):
    ids, candidate, calls = managed
    # Independent opens, real flock; only the public entry UID is simulated.
    original_uid = os.geteuid

    def web_lock():
        monkeypatch.setattr(os, "geteuid", lambda: ids[1])
        return operation.operation_lock()

    if first == "web":
        with web_lock():
            monkeypatch.setattr(os, "geteuid", original_uid)
            with pytest.raises(BackupError, match="operation_busy"):
                engine.execute(candidate)
        assert calls == []
    else:
        with operation.restore_lock():
            with pytest.raises(BackupError, match="operation_busy"):
                with web_lock():
                    pass
        with web_lock():
            pass


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "fifo", "owner", "mode"])
def test_current_authority_rejects_before_systemd(managed, tmp_path, damage):
    ids, candidate, calls = managed
    target = capture.DATABASE
    if damage == "symlink":
        target.unlink()
        target.symlink_to(tmp_path / "missing")
    elif damage == "hardlink":
        os.link(target, tmp_path / "other")
    elif damage == "fifo":
        target.unlink()
        os.mkfifo(target, 0o660)
    elif damage == "owner":
        os.chown(target, 0, 0)
    else:
        target.chmod(0o666)
    with pytest.raises((BackupError, OSError)):
        engine.execute(candidate)
    assert calls == []


def test_fixed_authority_matches_installer():
    from test_native_install import host as install_host
    from test_native_install import services as install_services

    assert host.SERVICES == install_services.SERVICES == install_host.SERVICES
    assert host.AUXILIARY_UNITS == install_services.AUXILIARY_UNITS
    assert capture.DATABASE == install_host.DATABASE
    assert capture.KEY == install_host.KEY


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_signal_uses_failure_path(managed, monkeypatch, sig):
    ids, candidate, calls = managed
    original = before(ids)

    def interrupted(*args, **kwargs):
        os.kill(os.getpid(), sig)

    monkeypatch.setattr(engine, "validate_data", interrupted)
    with (
        engine.interruptions(),
        pytest.raises(BackupError, match="restore_failed_rolled_back"),
    ):
        engine.execute(candidate)
    assert before(ids) == original
    assert calls == ["stop", "stop"]


def test_restore_lock_does_not_create_and_public_entry_stays_web_only(managed):
    _, _, _ = managed
    lock = capture.KEY.parent / "backup.lock"
    lock.unlink()
    with pytest.raises(FileNotFoundError):
        with operation.restore_lock():
            pass
    assert not lock.exists()
    with pytest.raises(BackupError, match="web_identity_required"):
        with operation.operation_lock():
            pass


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "owner", "group", "mode"])
def test_restore_lock_requires_exact_existing_authority(managed, tmp_path, damage):
    _, candidate, calls = managed
    lock = capture.KEY.parent / "backup.lock"
    if damage == "symlink":
        lock.unlink()
        lock.symlink_to(tmp_path / "absent")
    elif damage == "hardlink":
        os.link(lock, tmp_path / "linked-lock")
    elif damage == "owner":
        os.chown(lock, 0, -1)
    elif damage == "group":
        os.chown(lock, -1, 0)
    else:
        lock.chmod(0o660)
    with pytest.raises((BackupError, OSError)):
        engine.execute(candidate)
    assert calls == []


def test_catalog_failure_discards_generated_sidecars_before_rollback(
    managed, monkeypatch
):
    ids, candidate, calls = managed
    original = before(ids)

    def fail(*args, **kwargs):
        for suffix in ("-wal", "-shm"):
            path = capture.DATABASE.with_name(capture.DATABASE.name + suffix)
            path.write_bytes(b"incoming stale SQLite bytes")
            os.chown(path, 0, ids[2])
            path.chmod(0o660)
        raise OSError("catalog invalidation failed")

    monkeypatch.setattr(engine, "validate_data", fail)
    with pytest.raises(BackupError, match="restore_failed_rolled_back"):
        engine.execute(candidate)
    assert before(ids) == original
    assert calls == ["stop", "stop"]
