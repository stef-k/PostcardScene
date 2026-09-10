"""Scheduled obligation, local serialization and exact-pair retention contracts."""

import os
import signal
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from test_backup import built as built
from test_backup import progress
from test_backup_policy import VERIFIED
from test_backup_policy import database as database

from postcardscene import backup
from postcardscene.backup import archive, destination, operation, retention
from postcardscene.backup import scheduled as runner
from postcardscene.backup.files import BackupError
from postcardscene.backup_policy import (
    get_backup_policy,
    get_backup_status,
    record_backup_attempt,
    record_backup_success,
    replace_backup_policy,
)
from postcardscene.settings import set_timezone


@pytest.fixture
def locked_host(tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(operation, "KEY", root / "session.key")
    monkeypatch.setattr(
        operation,
        "identities",
        lambda: (os.getuid(), os.getuid(), os.getgid(), os.getgid()),
    )
    return root / "backup.lock"


@pytest.mark.parametrize(
    "reason",
    [
        "disabled",
        "before_hour",
        "satisfied",
        "timezone_unavailable",
        "policy_unavailable",
    ],
)
def test_no_destination_io_for_noop_or_unavailable(
    database, locked_host, monkeypatch, reason
):
    now = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
    replace_backup_policy(
        database,
        reason != "disabled",
        "/inaccessible/private",
        23 if reason == "before_hour" else 0,
        7,
    )
    if reason == "satisfied":
        record_backup_attempt(database, 1)
        record_backup_success(
            database, VERIFIED, succeeded_at_ns=2, local_date="2026-09-10"
        )
    if reason == "timezone_unavailable":
        with database.transaction(write=True) as session:
            session.execute(
                text("UPDATE application_settings SET timezone='Unknown/Zone'")
            )
    if reason == "policy_unavailable":
        with database.transaction(write=True) as session:
            session.execute(text("DELETE FROM backup_policy"))
    monkeypatch.setattr(runner, "Database", lambda: database)
    monkeypatch.setattr(runner, "datetime", type("Clock", (), {"now": lambda _: now}))
    monkeypatch.setattr(backup, "_run", lambda *a, **kw: pytest.fail("destination I/O"))
    monkeypatch.setattr(runner, "_run", lambda *a, **kw: pytest.fail("destination I/O"))
    if reason.endswith("unavailable"):
        with pytest.raises(BackupError, match=reason):
            runner.scheduled()
    else:
        assert runner.scheduled() == {"result": reason}
        assert get_backup_policy(database)[1].last_attempt_ns == (
            1 if reason == "satisfied" else None
        )


def test_one_frozen_snapshot_and_continuous_nonrecursive_lock(
    database, locked_host, monkeypatch
):
    replace_backup_policy(database, True, "/original", 0, 2)
    monkeypatch.setattr(runner, "Database", lambda: database)
    status = get_backup_status(
        database,
        datetime(2026, 9, 10, 23, 59, tzinfo=timezone.utc),
        include_destination=True,
    )
    reads = []

    def read(*args, **kwargs):
        reads.append(args[1])
        assert kwargs == {"include_destination": True}
        return status

    monkeypatch.setattr(runner, "get_backup_status", read)
    operations = []

    def worker(action, path, **kwargs):
        with pytest.raises(BackupError, match="operation_busy"):
            backup.create("/manual")
        assert path == "/original"
        operations.append(action)
        if action == "create":
            # These writes also prove no transaction spans destination work.
            replace_backup_policy(database, False, "/changed", 23, 30)
            set_timezone(database, "Pacific/Auckland")
            return vars(VERIFIED)
        assert kwargs["arguments"] == (
            "2",
            VERIFIED.archive_filename,
            VERIFIED.archive_sha256,
        )
        assert get_backup_policy(database)[1].last_success_local_date == "2026-09-10"

    monkeypatch.setattr(backup, "_run", worker)
    monkeypatch.setattr(runner, "_run", worker)
    assert runner.scheduled()["result"] == "ready"
    assert operations == ["create", "retain"] and len(reads) == 1
    with operation.operation_lock():
        pass
    assert (
        get_backup_policy(database)[1].last_archive_filename
        == VERIFIED.archive_filename
    )


@pytest.mark.parametrize("failure", [None, "create", "retain"])
def test_repeat_success_and_failure_preserve_history(
    database, locked_host, monkeypatch, failure
):
    replace_backup_policy(database, True, "/destination", 0, 1)
    record_backup_attempt(database, 1)
    record_backup_success(
        database, VERIFIED, succeeded_at_ns=2, local_date="2026-09-09"
    )
    monkeypatch.setattr(runner, "Database", lambda: database)
    monkeypatch.setattr(
        runner,
        "datetime",
        type(
            "Clock",
            (),
            {"now": lambda _: datetime(2026, 9, 10, 12, tzinfo=timezone.utc)},
        ),
    )
    new = replace(
        VERIFIED, archive_filename=VERIFIED.archive_filename.replace("030000", "120000")
    )
    calls = []

    def worker(action, *args, **kw):
        calls.append(action)
        if action == failure:
            raise BackupError("backup_failed")
        return vars(new) if action == "create" else {}

    monkeypatch.setattr(backup, "_run", worker)
    monkeypatch.setattr(runner, "_run", worker)
    if failure == "create":
        with pytest.raises(BackupError):
            runner.scheduled()
        history = get_backup_policy(database)[1]
        assert history.last_result == "failed"
        assert history.last_archive_filename == VERIFIED.archive_filename
        assert history.last_success_local_date == "2026-09-09"
    else:
        expected = "ready_retention_degraded" if failure else "ready"
        assert runner.scheduled()["result"] == expected
        assert get_backup_policy(database)[1].last_result == expected
        assert runner.scheduled() == {"result": "satisfied"}
        assert calls == ["create", "retain"]


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "mode", "fifo"])
def test_lock_rejects_untrusted_file(locked_host, damage):
    if damage == "fifo":
        os.mkfifo(locked_host, 0o600)
    elif damage == "symlink":
        locked_host.symlink_to(locked_host.parent / "missing")
    else:
        locked_host.touch(mode=0o600)
        if damage == "hardlink":
            os.link(locked_host, locked_host.parent / "alias")
        else:
            locked_host.chmod(0o644)
    with pytest.raises((BackupError, OSError)):
        with operation.operation_lock():
            pytest.fail("untrusted lock accepted")


def test_lock_releases_after_process_crash(locked_host):
    code = """
import os, sys, time
from pathlib import Path
from postcardscene.backup import operation
operation.KEY = Path(sys.argv[1]) / 'session.key'
operation.identities = lambda: (os.getuid(), os.getuid(), os.getgid(), os.getgid())
with operation.operation_lock():
    print('locked', flush=True)
    time.sleep(30)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(locked_host.parent)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline() == "locked\n"
        with pytest.raises(BackupError, match="operation_busy"):
            backup.create("/unused")
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=5)
        with operation.operation_lock():
            assert locked_host.stat().st_mode & 0o777 == 0o600
    finally:
        child.kill()
        child.wait(timeout=5)
        child.stdout.close()


def history_pairs(built):
    scratch, path, _ = built
    pairs = []
    for day in range(1, 5):
        source = archive.build(
            scratch, datetime(2026, 9, day, tzinfo=timezone.utc), "0.1.0.dev0", progress
        )
        pairs.append(destination.publish(source, path.parent, progress))
    return path, pairs


def test_retention_keeps_newest_owned_pairs_and_preserves_unowned(built, monkeypatch):
    path, pairs = history_pairs(built)
    foreign = [
        "foreign",
        ".postcardscene-backup-temp",
        pairs[0].archive_filename.replace("20260901", "20260905"),
        pairs[0].archive_filename.replace("20260901", "20260906"),
    ]
    for name in foreign:
        (path.parent / name).write_bytes(b"keep")
    malformed = path.parent / foreign[-1]
    (path.parent / (malformed.name + ".sha256")).write_text("invalid")
    before = {
        p.name: p.read_bytes()
        for p in path.parent.iterdir()
        if p.name in {*foreign, malformed.name + ".sha256"}
    }
    monkeypatch.setattr(
        archive,
        "check_database",
        lambda *a: pytest.fail("historical SQLite verification"),
    )
    new = built[2]
    retention.retain(path.parent, 2, new.archive_filename, new.archive_sha256, progress)
    assert path.exists()
    assert (path.parent / pairs[-1].archive_filename).exists()
    for item in pairs[:-1]:
        assert not (path.parent / item.archive_filename).exists()
        assert not (path.parent / (item.archive_filename + ".sha256")).exists()
    assert all(
        (path.parent / name).read_bytes() == data for name, data in before.items()
    )


@pytest.mark.parametrize("damage", ["unlink", "replace", "new_missing"])
def test_retention_stops_uncertain_deletion_and_preserves_new(
    built, monkeypatch, damage
):
    path, pairs = history_pairs(built)
    new = built[2]
    original = retention.pair_identity
    calls = 0

    def changed(parent, name):
        nonlocal calls
        value = original(parent, name)
        if name == new.archive_filename:
            calls += 1
            if calls >= 3 and damage == "new_missing":
                raise BackupError("retention_authority_changed")
        if name == pairs[0].archive_filename and damage == "replace" and calls >= 3:
            return ((), ())
        return value

    monkeypatch.setattr(retention, "pair_identity", changed)
    if damage == "unlink":
        monkeypatch.setattr(
            retention.os,
            "unlink",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("private path")),
        )
    with pytest.raises((OSError, BackupError)):
        retention.retain(
            path.parent, 1, new.archive_filename, new.archive_sha256, progress
        )
    assert path.exists()
    assert all((path.parent / item.archive_filename).exists() for item in pairs)


def test_mutation_worker_retains_lock_after_controller_death(locked_host):
    import time

    code = """
import os, sys
from pathlib import Path
from postcardscene import backup
from postcardscene.backup import operation
operation.KEY = Path(sys.argv[1]) / 'session.key'
operation.identities = lambda: (os.getuid(), os.getuid(), os.getgid(), os.getgid())
original = backup.subprocess.Popen

def worker(*args, **kwargs):
    child = original([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
    print(child.pid, flush=True)
    return child

backup.subprocess.Popen = worker
backup.create('/unused')
"""
    controller = subprocess.Popen(
        [sys.executable, "-c", code, str(locked_host.parent)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    worker_pid = None
    try:
        worker_pid = int(controller.stdout.readline())
        controller.kill()
        controller.wait(timeout=5)
        with pytest.raises(BackupError, match="operation_busy"):
            backup.create("/unused")
        os.kill(worker_pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while True:
            try:
                with operation.operation_lock():
                    break
            except BackupError:
                assert time.monotonic() < deadline
                time.sleep(0.01)
    finally:
        controller.kill()
        controller.wait(timeout=5)
        controller.stdout.close()
        if worker_pid is not None:
            try:
                os.kill(worker_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
