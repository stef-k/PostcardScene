"""Recovery selection, inherited lock and read-only interrupted database evidence."""

import json
import os
import sqlite3
import sys
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from test_native_install import bundle as bundle
from test_native_install import installer


@pytest.fixture
def recovery(bundle):
    installer.load_support(bundle)
    return sys.modules["postcardscene_install_update_recovery"]


def verified(recovery):
    return recovery.VerifiedBackup(
        "postcardscene-backup-20200101T000000000000Z-v0.1.0.dev0.tar.gz",
        "a" * 64,
        "2020-01-01T00:00:00Z",
        "0.1.0.dev0",
        0x5053434E,
        "0012_backup_policy",
        "regenerable_reconcile_required",
    )


@pytest.mark.parametrize(
    "field",
    [
        "archive_filename",
        "archive_sha256",
        "created_at_utc",
        "application_version",
        "sqlite_application_id",
        "schema_revision",
        "catalog_classification",
    ],
)
def test_reverify_compares_complete_identity(recovery, field):
    value = verified(recovery)
    current = dict(
        version=value.application_version,
        schema=value.schema_revision,
        application_id=value.sqlite_application_id,
    )
    returned = value
    point = recovery.Recovery(
        current, 7, lambda *a, **kw: json.dumps(asdict(returned)).encode()
    )
    point.select(archive="/backup/" + value.archive_filename)
    returned = replace(
        value, **{field: 42 if field == "sqlite_application_id" else "changed"}
    )
    with pytest.raises(recovery.InstallError, match="identity_mismatch"):
        point.reverify()


def test_explicit_archive_age_and_destination_do_not_use_status(recovery):
    value = verified(recovery)
    calls = []

    def run(args, **kw):
        calls.append(args)
        assert args[4] == recovery.VERIFY
        return json.dumps(asdict(value)).encode()

    current = dict(
        version=value.application_version,
        schema=value.schema_revision,
        application_id=value.sqlite_application_id,
    )
    point = recovery.Recovery(current, 7, run)
    assert point.select(archive="/backup/" + value.archive_filename) == value
    assert len(calls) == 1  # Explicitly selected 2020 archive has no hidden age gate.
    with pytest.raises(recovery.InstallError, match="selection_conflict"):
        point.select("/backup", "/backup/archive")


def test_fixed_old_create_expression_uses_already_locked_api(
    recovery, monkeypatch, capsys
):
    import postcardscene.backup as backup

    value = verified(recovery)
    calls = []

    def create(destination, *, lock_fd):
        calls.append((destination, lock_fd))
        return value

    monkeypatch.setattr(backup, "_create_locked", create)
    monkeypatch.setattr(backup, "create", lambda *a: pytest.fail("reacquired lock"))
    monkeypatch.setattr(sys, "argv", ["-c", "/backup", "7"])
    exec(recovery.CREATE, {})
    assert calls == [("/backup", 7)]
    assert recovery.result(capsys.readouterr().out) == value


@pytest.mark.skipif(os.geteuid() != 0, reason="root lock bootstrap")
@pytest.mark.parametrize("damage", [None, "mode", "link", "symlink", "parent"])
def test_update_and_restore_contend_on_exact_same_inode(
    recovery, tmp_path, monkeypatch, damage
):
    from postcardscene.backup import operation

    root = tmp_path / "web"
    root.mkdir(mode=0o700)
    key = root / "session.key"
    ids = (os.getuid(), os.getuid(), os.getgid(), os.getgid())
    monkeypatch.setattr(recovery.host, "KEY", key)
    monkeypatch.setattr(recovery.host, "preserved_identities", lambda: ids)
    # Test /tmp ancestor only; protected private parent validation remains real.
    monkeypatch.setattr(recovery.host, "trusted_parent", lambda p: None)
    monkeypatch.setattr(operation, "KEY", key)
    monkeypatch.setattr(operation, "identities", lambda: ids)
    lock = root / "backup.lock"
    with recovery.mutation_lock() as fd:
        info = os.fstat(fd)
        with pytest.raises(operation.BackupError, match="operation_busy"):
            with operation.restore_lock():
                pass
    assert lock.stat().st_ino == info.st_ino
    if damage == "mode":
        lock.chmod(0o644)
    elif damage == "link":
        os.link(lock, root / "alias")
    elif damage == "symlink":
        lock.rename(root / "original")
        lock.symlink_to(root / "original")
    elif damage == "parent":
        root.chmod(0o755)
    if damage:
        before = lock.lstat()
        with pytest.raises((recovery.InstallError, OSError)):
            with recovery.mutation_lock():
                pass
        assert lock.lstat() == before
    else:
        with operation.restore_lock():
            with pytest.raises(recovery.InstallError, match="operation_busy"):
                with recovery.mutation_lock():
                    pass


@pytest.mark.parametrize(
    "head", ["old", "target", "unknown", "corrupt", "foreign", "multiple"]
)
def test_database_classification_uses_private_main_and_wal(
    recovery, tmp_path, monkeypatch, head
):
    database = sys.modules["postcardscene_install_update_database"]
    data_root = tmp_path / "database"
    data_root.mkdir()
    path = data_root / "state.sqlite3"
    monkeypatch.setattr(database.host, "DATABASE", path)
    state = SimpleNamespace(from_schema="old", to_schema="target")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        f"PRAGMA application_id={0x5053434E if head != 'foreign' else 12}"
    )
    connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
    connection.execute("INSERT INTO alembic_version VALUES (?)", (head,))
    if head == "multiple":
        connection.execute("INSERT INTO alembic_version VALUES ('old')")
    connection.commit()
    if head == "corrupt":
        connection.close()
        path.write_bytes(b"not SQLite")
    paths = list(data_root.iterdir())
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
    try:
        if head in {"old", "target"}:
            assert database.classify(state, 0x5053434E) == head
            if head == "old":
                assert (
                    database.classify(
                        SimpleNamespace(from_schema="old", to_schema="old"), 0x5053434E
                    )
                    == "old"
                )
        else:
            with pytest.raises(database.InstallError, match="unrecognized"):
                database.classify(state, 0x5053434E)
        assert set(data_root.iterdir()) == set(paths)
        assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths} == before
    finally:
        connection.close()


def test_managed_runner_inherits_fd_and_bounds_capture(recovery, tmp_path):
    command = sys.modules["postcardscene_install_command"]
    path = tmp_path / "lock"
    with path.open("w+b") as stream:
        fd = stream.fileno()
        output = command.command(
            (
                sys.executable,
                "-I",
                "-c",
                "import os,sys; print(os.fstat(int(sys.argv[1])).st_ino)",
                str(fd),
            ),
            capture=True,
            pass_fds=(fd,),
        )
        assert int(output) == path.stat().st_ino
    with pytest.raises(command.InstallError, match="output_invalid"):
        command.command((sys.executable, "-I", "-c", "print('a'*70000)"), capture=True)
    with pytest.raises(command.InstallError, match="timeout"):
        command.command(
            (sys.executable, "-I", "-c", "import time; time.sleep(10)"),
            capture=True,
            timeout=0.1,
        )


def test_captured_managed_command_keeps_fixed_web_environment(recovery, monkeypatch):
    command = sys.modules["postcardscene_install_command"]
    real_popen = command.subprocess.Popen
    account = SimpleNamespace(
        pw_uid=12002, pw_gid=12001, pw_dir="/var/lib/postcardscene-web"
    )
    monkeypatch.setattr(command.pwd, "getpwnam", lambda user: account)

    def launch(args, **kw):
        assert kw.pop("user") == 12002
        assert kw.pop("group") == 12001
        assert kw.pop("extra_groups") == []
        assert kw["cwd"] == "/" and kw["umask"] == 0o007
        assert kw["start_new_session"] is True
        assert kw["env"] == {
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "LC_ALL": "C",
            "LANG": "C",
            "HOME": account.pw_dir,
            "POSTCARDSCENE_CONFIG": "/etc/postcardscene/config.py",
        }
        # Identity switching itself is covered by the existing privileged installed
        # lane. Here the real child proves the captured environment and cwd.
        return real_popen(args, **kw)

    monkeypatch.setattr(command.subprocess, "Popen", launch)
    output = command.command(
        (
            sys.executable,
            "-I",
            "-c",
            "import os; print(os.getcwd()); print(os.environ['POSTCARDSCENE_CONFIG'])",
        ),
        user="postcardscene-web",
        capture=True,
    )
    assert output == b"/\n/etc/postcardscene/config.py\n"
