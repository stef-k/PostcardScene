"""Root phase/lock contracts through committed, with systemd and CLI seams isolated."""

import json
import os
import sys
from dataclasses import asdict, replace

import pytest
from test_update_host import bundle as bundle
from test_update_host import make_state, populate_release
from test_update_host import managed as managed

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0, reason="requires root file ownership"
)


@pytest.fixture
def transaction(managed, monkeypatch, tmp_path):
    update, target, current, preflight = managed
    engine = sys.modules["postcardscene_install_update_transaction"]
    recovery = engine.recovery
    release = update.host.RELEASES / target.version
    release.mkdir()
    populate_release(release, target.version, target.wheel, target.requirements)
    identity, _ = update.stored_release(current.name)
    monkeypatch.setattr(
        update,
        "prepare_target",
        lambda *a: (identity, target, update.state_files.read()),
    )
    web = tmp_path / "private"
    web.mkdir(mode=0o700)
    monkeypatch.setattr(update.host, "KEY", web / "session.key")
    monkeypatch.setattr(update.host, "preserved_identities", lambda: (0, 0, 0, 0))
    monkeypatch.setattr(update.host, "require_no_processes", lambda ids: None)
    events = []
    oneshot = update.host.AUXILIARY_UNITS[0]
    states = {
        unit: {
            "LoadState": "loaded",
            "ActiveState": "active",
            "UnitFileState": "static" if unit == oneshot else "enabled",
        }
        for unit in (*update.host.SERVICES, *update.host.AUXILIARY_UNITS)
    }
    monkeypatch.setattr(update.host, "service_state", lambda pf, unit: states[unit])
    verified = recovery.VerifiedBackup(
        "postcardscene-backup-20200101T000000000000Z-v0.1.0.dev0.tar.gz",
        "a" * 64,
        "2020-01-01T00:00:00Z",
        identity["version"],
        identity["application_id"],
        identity["schema"],
        "regenerable_reconcile_required",
    )

    def run(args, **options):
        # Every mutation/verification happens under the canonical inode, and the
        # caller's committed context continues to own it.
        with pytest.raises(recovery.InstallError, match="operation_busy"):
            with recovery.mutation_lock():
                pass
        if args[0] == "/usr/bin/systemctl":
            action, unit = args[1:]
            events.append((action, unit))
            states[unit]["ActiveState" if action == "stop" else "UnitFileState"] = (
                "inactive" if action == "stop" else "disabled"
            )
        elif args[3] == "-c":
            code = args[4]
            events.append(
                "create"
                if code == recovery.CREATE
                else "verify"
                if code == recovery.VERIFY
                else "policy"
            )
            assert args[0] == str(current / "venv/bin/python")
            assert options["user"] == "postcardscene-web"
            fd = options["pass_fds"][0]
            assert os.path.samestat(os.fstat(fd), (web / "backup.lock").stat())
            if code == recovery.CREATE:
                assert args[-1] == str(fd)
            return json.dumps(
                "/backup" if code == recovery.POLICY else asdict(verified)
            ).encode()
        else:
            events.append(args[-1])
            assert args == (
                str(release / "venv/bin/python"),
                "-I",
                "-m",
                "flask",
                "--app",
                "postcardscene.web:create_app",
                "db",
                args[-1],
            )
            assert options["user"] == "postcardscene-web"
            assert options["timeout"] == 300 and options["pass_fds"]
            assert all(v["ActiveState"] == "inactive" for v in states.values())
            if args[-1] == "check":
                return json.dumps(
                    {
                        "application": "postcardscene",
                        "application_version": target.version,
                        "sqlite_application_id": target.identity["application_id"],
                        "schema_revision": target.identity["schema"],
                    }
                ).encode()

    return engine, target, current, preflight, run, events, states, verified


def test_fresh_gate_and_context_retain_lock_through_commit(transaction):
    engine, target, current, pf, run, events, states, _ = transaction
    with engine.transaction(target, pf, run=run) as committed:
        assert committed.phase == "committed"
        with pytest.raises(engine.InstallError, match="operation_busy"):
            with engine.recovery.mutation_lock():
                pass
        assert engine.update.state_files.read() == committed
        assert engine.update.active_version() == current.name
    with engine.recovery.mutation_lock():
        pass
    expected = [
        ("stop", engine.host.AUXILIARY_UNITS[1]),
        ("disable", engine.host.AUXILIARY_UNITS[1]),
        ("stop", engine.host.AUXILIARY_UNITS[0]),
    ]
    expected += [
        (action, unit)
        for unit in reversed(engine.host.SERVICES)
        for action in ("stop", "disable")
    ]
    assert events == ["policy", "create", "verify", *expected, "upgrade", "check"]
    assert all(v["UnitFileState"] in {"disabled", "static"} for v in states.values())


@pytest.mark.parametrize("failure", ["lock", "create", "verify", "mismatch"])
def test_recovery_failure_has_no_phase_or_host_mutation(
    transaction, monkeypatch, failure
):
    engine, target, current, pf, run, events, _, verified = transaction
    original_lock = engine.recovery.mutation_lock
    if failure == "lock":
        monkeypatch.setattr(
            engine.recovery,
            "mutation_lock",
            lambda: (_ for _ in ()).throw(engine.InstallError("busy")),
        )

    def fail(args, **kw):
        if len(args) > 4 and args[4] == getattr(engine.recovery, failure.upper(), None):
            raise engine.InstallError("injected")
        if (
            failure == "mismatch"
            and len(args) > 4
            and args[4] == engine.recovery.VERIFY
        ):
            return json.dumps(
                asdict(replace(verified, archive_sha256="b" * 64))
            ).encode()
        return run(args, **kw)

    with pytest.raises(engine.InstallError):
        with engine.transaction(target, pf, run=fail):
            pytest.fail("committed")
    assert engine.update.state_files.read() is None
    assert not (engine.host.RELEASES / target.version).exists()
    assert current.exists() and not any(isinstance(e, tuple) for e in events)
    monkeypatch.setattr(engine.recovery, "mutation_lock", original_lock)


@pytest.mark.parametrize("boundary", ["prepared", "stop", "upgrade", "check"])
def test_hard_interruption_rerun_retires_before_migration(
    transaction, monkeypatch, boundary
):
    engine, target, current, pf, run, events, _, _ = transaction
    files = engine.update.state_files
    write = files.write

    class Killed(BaseException):
        pass

    def persist(state):
        write(state)
        if boundary == "prepared" and state.phase == "prepared":
            raise Killed

    def interrupted(args, **kw):
        value = run(args, **kw)
        if (boundary == "stop" and args[0] == "/usr/bin/systemctl") or args[
            -1
        ] == boundary:
            raise Killed
        return value

    monkeypatch.setattr(files, "write", persist)
    with pytest.raises(Killed):
        with engine.transaction(target, pf, run=interrupted):
            pass
    assert files.read().phase == (
        "prepared" if boundary in {"prepared", "stop"} else "migrating"
    )
    assert engine.update.active_version() == current.name
    monkeypatch.setattr(files, "write", write)
    monkeypatch.setattr(engine.database, "classify", lambda *a: "old")
    events.clear()
    with engine.transaction(target, pf, run=run):
        pass
    assert events[-2:] == ["upgrade", "check"]
    assert events.index(("stop", engine.host.SERVICES[0])) < events.index("upgrade")


@pytest.mark.parametrize("failure", ["stop", "state", "processes"])
def test_quiescence_failure_attempts_every_retirement(
    transaction, monkeypatch, failure
):
    engine, target, _, pf, run, events, _, _ = transaction

    def fail(args, **kw):
        value = run(args, **kw)
        if failure == "stop" and args[0] == "/usr/bin/systemctl":
            raise OSError
        return value

    if failure == "state":
        monkeypatch.setattr(
            engine.host, "service_state", lambda *a: (_ for _ in ()).throw(OSError())
        )
    if failure == "processes":
        monkeypatch.setattr(
            engine.host,
            "require_no_processes",
            lambda *a: (_ for _ in ()).throw(OSError()),
        )
    with pytest.raises(engine.InstallError, match="quiescence"):
        with engine.transaction(target, pf, run=fail):
            pass
    assert len([e for e in events if isinstance(e, tuple)]) == 9
    assert engine.update.state_files.read().phase == "prepared"
    assert "upgrade" not in events


@pytest.mark.parametrize("head", ["old", "target", "unknown"])
def test_interrupted_cross_schema_classification(transaction, monkeypatch, head):
    engine, target, current, pf, run, events, _, _ = transaction
    state = replace(
        make_state(engine.update, target, current),
        phase="migrating",
        from_schema="old_head",
    )
    # The fixture target represents a later schema for this decision test; live
    # database recognition and integrity are tested separately with real SQLite.
    current_id, _ = engine.update.stored_release(current.name)
    current_id["schema"] = "old_head"
    monkeypatch.setattr(
        engine.update, "prepare_target", lambda *a: (current_id, target, state)
    )
    engine.update.state_files.write(state)
    monkeypatch.setattr(
        engine.database,
        "classify",
        lambda *a: (
            head
            if head != "unknown"
            else (_ for _ in ()).throw(engine.InstallError("unknown"))
        ),
    )
    calls = []
    monkeypatch.setattr(
        engine.recovery.Recovery, "select", lambda *a: calls.append("new recovery")
    )
    if head == "unknown":
        with pytest.raises(engine.InstallError):
            with engine.transaction(target, pf, run=run):
                pass
        assert engine.update.state_files.read().phase == "migrating"
        assert not calls and "upgrade" not in events
    else:
        with engine.transaction(target, pf, run=run):
            pass
        assert calls == (["new recovery"] if head == "old" else [])
        assert ("upgrade" in events) == (head == "old")
        assert events[-1] == "check"


def test_bad_target_check_never_commits(transaction):
    engine, target, _, pf, run, _, _, _ = transaction

    def bad(args, **kw):
        result = run(args, **kw)
        return b"{}" if args[-1] == "check" else result

    with pytest.raises(engine.InstallError, match="database_check"):
        with engine.transaction(target, pf, run=bad):
            pass
    assert engine.update.state_files.read().phase == "migrating"
