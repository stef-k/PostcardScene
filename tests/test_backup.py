"""Backup product contracts on disposable local files, without managed host mutation."""

import hashlib
import io
import json
import os
import sqlite3
import stat
import tarfile
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

import pytest

from postcardscene import backup
from postcardscene.backup import archive, capture, destination
from postcardscene.backup.files import DB_NAME, MANIFEST_NAME, BackupError
from postcardscene.schema import upgrade_database


def progress():
    pass


@pytest.fixture
def built(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    upgrade_database(scratch / DB_NAME)
    (scratch / DB_NAME).chmod(0o600)
    (scratch / "config.py").write_bytes(b'TRUSTED_HOSTS = ["private.example"]\n')
    (scratch / "session.key").write_bytes(bytes(range(32)))
    for name in ("config.py", "session.key"):
        (scratch / name).chmod(0o600)
    path = archive.build(
        scratch, datetime(2026, 9, 10, tzinfo=timezone.utc), "0.1.0.dev0", progress
    )
    output = tmp_path / "destination"
    output.mkdir()
    result = destination.publish(path, output, progress)
    return scratch, output / path.name, result


def rewrite(path, transform):
    with tarfile.open(path) as source:
        members = [(info, source.extractfile(info).read()) for info in source]
    members = transform(members)
    with tarfile.open(path, "w:gz", format=tarfile.USTAR_FORMAT) as target:
        for info, value in members:
            info.size = len(value)
            target.addfile(info, io.BytesIO(value))
    sidecar(path)


def sidecar(path):
    path.with_name(path.name + ".sha256").write_text(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
    )


def test_fixed_sensitive_archive_round_trip_and_cli(built, capsys):
    from postcardscene.backup.cli import main

    scratch, path, expected = built
    before = {p.name: p.read_bytes() for p in path.parent.iterdir()}
    with tarfile.open(path) as content:
        assert set(content.getnames()) == {
            DB_NAME,
            "config.py",
            "session.key",
            MANIFEST_NAME,
        }
        for name in ("config.py", "session.key"):
            assert content.extractfile(name).read() == (scratch / name).read_bytes()
        manifest = json.load(content.extractfile(MANIFEST_NAME))
        assert manifest["catalog_classification"] == archive.CATALOG
        assert "private.example" not in json.dumps(manifest)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.with_name(path.name + ".sha256").stat().st_mode) == 0o600
    for _ in range(2):
        assert backup.verify(path) == expected
        assert backup.list_backups(path.parent) == (
            destination.BackupEntry(path.name, "verified"),
        )
    with pytest.raises(FrozenInstanceError):
        expected.schema_revision = "other"
    assert main(["verify", str(path)]) == 0
    output = capsys.readouterr().out
    assert "private.example" not in output
    assert str(scratch) not in output
    assert expected.archive_sha256 in output
    assert before == {p.name: p.read_bytes() for p in path.parent.iterdir()}


def test_valid_pair_substitution_changes_complete_recovery_identity(built):
    scratch, path, _ = built
    selected = backup.verify(path)
    (scratch / "session.key").write_bytes(b"x" * 32)
    (scratch / path.name).unlink()
    substitute = archive.build(
        scratch, datetime(2026, 9, 10, tzinfo=timezone.utc), "0.1.0.dev0", progress
    )
    path.write_bytes(substitute.read_bytes())
    sidecar(path)
    rechecked = backup.verify(path)
    assert rechecked.archive_filename == selected.archive_filename
    assert rechecked.archive_sha256 != selected.archive_sha256
    assert rechecked != selected


@pytest.mark.parametrize(
    "damage", ["missing", "wrong", "archive", "member", "manifest", "database"]
)
def test_corruption_fails_and_lists_invalid(built, damage):
    _, path, _ = built
    if damage == "missing":
        path.with_name(path.name + ".sha256").unlink()
    elif damage == "wrong":
        path.with_name(path.name + ".sha256").write_text("0" * 64 + f"  {path.name}\n")
    elif damage == "archive":
        with path.open("ab") as target:
            target.write(b"changed")
    else:

        def change(members):
            result = []
            for info, value in members:
                if damage == "member" and info.name == "config.py":
                    value = b"altered config"
                if damage == "database" and info.name == DB_NAME:
                    value = b"not sqlite"
                if info.name == MANIFEST_NAME:
                    data = json.loads(value)
                    if damage == "manifest":
                        data["extra"] = "forbidden"
                    if damage == "database":
                        data["members"][DB_NAME] = {
                            "size": 10,
                            "sha256": hashlib.sha256(b"not sqlite").hexdigest(),
                        }
                    value = json.dumps(data).encode()
                result.append((info, value))
            return result

        rewrite(path, change)
    with pytest.raises(BackupError):
        backup.verify(path)
    state = "incomplete" if damage == "missing" else "invalid"
    assert backup.list_backups(path.parent)[0].state == state


@pytest.mark.parametrize(
    "kind",
    [
        "extra",
        "duplicate",
        "traversal",
        "absolute",
        "symlink",
        "hardlink",
        "device",
        "pax",
    ],
)
def test_unsafe_members_reject_before_extraction(built, tmp_path, kind):
    _, path, _ = built

    def change(members):
        name = {
            "traversal": "../outside",
            "absolute": "/outside",
            "duplicate": DB_NAME,
        }.get(
            kind,
            DB_NAME if kind in {"symlink", "hardlink", "device", "pax"} else "extra",
        )
        info = tarfile.TarInfo(name)
        info.mode = 0o600
        info.type = {
            "symlink": tarfile.SYMTYPE,
            "hardlink": tarfile.LNKTYPE,
            "device": tarfile.CHRTYPE,
            "pax": tarfile.XHDTYPE,
        }.get(kind, tarfile.REGTYPE)
        if kind in {"symlink", "hardlink"}:
            info.linkname = DB_NAME
        return (members if kind in {"extra", "duplicate"} else members[1:]) + [
            (info, b"x")
        ]

    rewrite(path, change)
    scratch = tmp_path / "unpack"
    scratch.mkdir(mode=0o700)
    with pytest.raises((BackupError, tarfile.TarError)):
        archive.unpack(path, scratch, progress)
    assert list(scratch.iterdir()) == []
    with pytest.raises(BackupError):
        backup.verify(path)


def test_partial_collision_foreign_and_bounded_listing(built, tmp_path, monkeypatch):
    scratch, path, expected = built
    with pytest.raises(BackupError):
        destination.publish(scratch / path.name, path.parent, progress)
    assert backup.verify(path) == expected
    (path.parent / "foreign").symlink_to("/unavailable")
    (path.parent / ".postcardscene-backup-orphan").write_bytes(b"ignored")
    assert len(backup.list_backups(path.parent)) == 1
    original = destination.rename_exclusive

    def interrupt(parent, old, new):
        if new.endswith(".sha256"):
            raise OSError("publication interruption")
        original(parent, old, new)

    monkeypatch.setattr(destination, "rename_exclusive", interrupt)
    partial = tmp_path / "partial"
    partial.mkdir()
    with pytest.raises(OSError):
        destination.publish(scratch / path.name, partial, progress)
    assert backup.list_backups(partial)[0].state == "incomplete"
    monkeypatch.setattr(destination, "ENTRY_LIMIT", 1)
    with pytest.raises(BackupError, match="entry_limit"):
        destination.list_backups(path.parent, progress)


def test_unavailable_symlink_and_relative_destinations_fail(built, tmp_path):
    _, path, _ = built
    symlink = tmp_path / "link"
    symlink.symlink_to(path.parent, target_is_directory=True)
    for invalid in (tmp_path / "missing", symlink, Path("relative"), path):
        with pytest.raises(BackupError):
            backup.list_backups(invalid)
    assert not (tmp_path / "missing").exists()


def test_worker_stall_and_cancellation_are_bounded(tmp_path, monkeypatch):
    import subprocess
    import sys
    import time

    original = subprocess.Popen
    children = []

    def stalled(*args, **kwargs):
        process = original(
            [sys.executable, "-c", "import time; time.sleep(60)"], **kwargs
        )
        children.append(process)
        return process

    monkeypatch.setattr(backup.subprocess, "Popen", stalled)
    before = set(tmp_path.iterdir())
    started = time.monotonic()
    with pytest.raises(BackupError, match="timeout"):
        backup.list_backups(tmp_path, idle_timeout=0.1)
    with pytest.raises(BackupError, match="cancelled"):
        backup.list_backups(tmp_path, cancelled=lambda: True)
    assert time.monotonic() - started < 3
    assert all(child.poll() is not None for child in children)
    assert set(tmp_path.iterdir()) == before


@pytest.fixture
def source_db(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir(mode=0o2770)
    root.chmod(0o2770)
    path = root / DB_NAME
    upgrade_database(path)
    path.chmod(0o660)
    monkeypatch.setattr(capture, "DATABASE", path)
    return path


def test_online_wal_capture_preserves_live_files_and_committed_state(
    source_db, tmp_path
):
    live = sqlite3.connect(source_db)
    live.execute("PRAGMA wal_autocheckpoint=0")
    live.execute("CREATE TABLE backup_probe (id INTEGER PRIMARY KEY, value BLOB)")
    live.executemany(
        "INSERT INTO backup_probe VALUES (?, ?)", [(i, b"x" * 8192) for i in range(200)]
    )
    live.commit()
    for suffix in ("-wal", "-shm"):
        Path(str(source_db) + suffix).chmod(0o660)
    before_db = source_db.read_bytes()
    before_wal = Path(str(source_db) + "-wal").read_bytes()
    writes = 0

    def writing():
        nonlocal writes
        if writes < 3:
            live.execute(
                "INSERT INTO backup_probe VALUES (?, ?)", (1000 + writes, b"new")
            )
            live.commit()
            writes += 1

    target = tmp_path / "snapshot.sqlite3"
    try:
        capture.snapshot(target, os.getuid(), os.getuid(), os.getgid(), writing)
        assert writes == 3
        with sqlite3.connect(target) as check:
            assert (
                check.execute("SELECT count(*) FROM backup_probe").fetchone()[0] == 203
            )
        assert source_db.read_bytes() == before_db
        assert Path(str(source_db) + "-wal").read_bytes().startswith(before_wal)
        unchanged = {
            p.name: p.read_bytes()
            for p in source_db.parent.iterdir()
            if not p.name.endswith("-shm")
        }
        capture.snapshot(
            tmp_path / "second.sqlite3", os.getuid(), os.getuid(), os.getgid(), progress
        )
        assert unchanged == {
            p.name: p.read_bytes()
            for p in source_db.parent.iterdir()
            if not p.name.endswith("-shm")
        }
    finally:
        live.close()


@pytest.mark.parametrize("damage", ["symlink", "mode", "owner", "damaged", "cancelled"])
def test_source_database_fails_closed(source_db, tmp_path, damage):
    runtime = os.getuid()
    if damage == "symlink":
        other = source_db.with_suffix(".other")
        source_db.rename(other)
        source_db.symlink_to(other)
    elif damage == "mode":
        source_db.chmod(0o666)
    elif damage == "owner":
        runtime += 1
    elif damage == "damaged":
        source_db.write_bytes(b"broken database")
    with pytest.raises((BackupError, OSError, sqlite3.Error)):
        capture.snapshot(
            tmp_path / "snapshot",
            runtime,
            os.getuid(),
            os.getgid(),
            progress,
            cancelled=lambda: damage == "cancelled",
        )


@pytest.mark.parametrize("damage", ["symlink", "mode", "owner", "hardlink", "oversize"])
def test_managed_config_and_key_metadata_fail_closed(tmp_path, damage):
    path = tmp_path / "authority"
    path.write_bytes(b"x" * 32)
    path.chmod(0o600)
    uid = os.getuid()
    if damage == "symlink":
        other = tmp_path / "target"
        path.rename(other)
        path.symlink_to(other)
    elif damage == "hardlink":
        os.link(path, tmp_path / "other")
    elif damage == "mode":
        path.chmod(0o640)
    elif damage == "owner":
        uid += 1
    else:
        path.write_bytes(b"x" * 33)
    with pytest.raises((BackupError, OSError)):
        capture.read_managed(
            path, (uid, os.getgid(), 0o600), (os.getuid(), os.getgid(), 0o700), 32
        )


@pytest.mark.parametrize(
    "config,key",
    [
        (b"import os", b"x" * 32),
        (b"DATABASE_PATH = '/elsewhere'", b"x" * 32),
        (b"TRUSTED_HOSTS = ['example']", b"short"),
    ],
)
def test_damaged_config_or_key_cannot_be_captured(tmp_path, monkeypatch, config, key):
    monkeypatch.setattr(
        capture,
        "identities",
        lambda: (os.getuid(), os.getuid(), os.getgid(), os.getgid()),
    )
    monkeypatch.setattr(
        capture,
        "read_managed",
        lambda path, *args: config if path == capture.CONFIG else key,
    )
    with pytest.raises(BackupError):
        capture.capture(tmp_path, progress)
    assert not (tmp_path / DB_NAME).exists()


def test_publication_does_not_create_or_fall_back_from_missing_destination(
    built, tmp_path
):
    scratch, path, _ = built
    missing = tmp_path / "absent" / "backup"
    before = set(tmp_path.iterdir())
    with pytest.raises(OSError):
        destination.publish(scratch / path.name, missing, progress)
    assert set(tmp_path.iterdir()) == before


def test_forged_checksum_does_not_hide_trailing_archive_content(built):
    import gzip

    _, path, _ = built
    with path.open("ab") as target:
        target.write(gzip.compress(b"hidden payload"))
    sidecar(path)
    with pytest.raises(BackupError):
        backup.verify(path)


def test_destination_ignoring_private_modes_gets_no_sensitive_bytes(
    built, tmp_path, monkeypatch
):
    scratch, path, _ = built
    output = tmp_path / "public-mode-mount"
    output.mkdir()
    original = os.open

    def ignores_mode(path, flags, mode=0o777, **options):
        fd = original(path, flags, mode, **options)
        if mode == 0o600 and "dir_fd" in options:
            os.fchmod(fd, 0o644)
        return fd

    monkeypatch.setattr(os, "open", ignores_mode)
    with pytest.raises(BackupError, match="private_output_unavailable"):
        destination.publish(scratch / path.name, output, progress)
    assert all(entry.stat().st_size == 0 for entry in output.iterdir())
    assert destination.list_backups(output, progress) == ()


def test_worker_ignores_hostile_cwd_and_pythonpath(built, tmp_path, monkeypatch):
    _, path, expected = built
    hostile = tmp_path / "hostile"
    package = hostile / "postcardscene" / "backup"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    marker = hostile / "shadow-executed"
    (package / "worker.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        "raise SystemExit(1)\n"
    )
    monkeypatch.chdir(hostile)
    monkeypatch.setenv("PYTHONPATH", str(hostile))
    try:
        assert backup.verify(path) == expected
        assert backup.list_backups(path.parent) == (
            destination.BackupEntry(path.name, "verified"),
        )
    finally:
        assert not marker.exists()
