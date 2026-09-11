"""Committed filesystem/rerun contracts with real root metadata and shared flock."""

import json
import os
import sys
from contextlib import contextmanager
from dataclasses import replace

import pytest
from test_update_host import bundle as bundle
from test_update_host import make_state, populate_release
from test_update_host import managed as managed

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="requires root ownership")


class Killed(BaseException):
    pass


@pytest.fixture
def committed(managed, monkeypatch, tmp_path):
    update, target, old, pf = managed
    finish = sys.modules["postcardscene_install_update_finish"]
    host = update.host
    release = host.RELEASES / target.version
    release.mkdir()
    populate_release(release, target.version, target.wheel, target.requirements)
    state = replace(make_state(update, target, old), phase="committed")
    update.state_files.write(state)
    old_assets = dict(host.asset_bytes(target.wheel))
    target_assets = {p: b"target-unit" for p in old_assets}
    monkeypatch.setattr(
        host,
        "asset_bytes",
        lambda wheel: target_assets if wheel == target.wheel else old_assets,
    )
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    monkeypatch.setattr(host, "KEY", private / "session.key")
    monkeypatch.setattr(host, "preserved_identities", lambda: (0, 0, 0, 0))
    monkeypatch.setattr(host, "require_no_processes", lambda _: None)
    states = {
        u: dict(
            LoadState="loaded",
            ActiveState="inactive",
            UnitFileState="static" if u == host.AUXILIARY_UNITS[0] else "disabled",
        )
        for u in (*host.SERVICES, *host.AUXILIARY_UNITS)
    }
    monkeypatch.setattr(host, "service_state", lambda pf, u: states[u])
    events = []
    released = False

    def run(args, **kw):
        nonlocal released
        if args[-1] == "check":
            released = False
        events.append(tuple(args))
        timer_start = args == ("/usr/bin/systemctl", "start", host.AUXILIARY_UNITS[1])
        if timer_start:
            released = True
        if released:
            with finish.transaction.recovery.mutation_lock():
                pass
        else:
            with pytest.raises(host.InstallError, match="operation_busy"):
                with finish.transaction.recovery.mutation_lock():
                    pass
        if args[0] == "/usr/bin/systemctl" and len(args) == 3:
            action, unit = args[1:]
            field, value = {
                "enable": ("UnitFileState", "enabled"),
                "disable": ("UnitFileState", "disabled"),
                "start": ("ActiveState", "active"),
                "stop": ("ActiveState", "inactive"),
            }[action]
            states[unit][field] = value
        if args[-1] == "check":
            assert kw["user"] == "postcardscene-web" and kw["pass_fds"]
            return json.dumps(
                dict(
                    application="postcardscene",
                    application_version=target.version,
                    sqlite_application_id=target.identity["application_id"],
                    schema_revision=target.identity["schema"],
                )
            ).encode()
        if args[-1] == "--json":
            assert "user" not in kw and kw["accepted_statuses"] == (0, 1)
            return json.dumps(
                dict(
                    exit_code=1,
                    checks=[dict(identifier=n, state="ready") for n in finish.CRITICAL]
                    + [dict(identifier="graphics", state="degraded")],
                )
            ).encode()
        assert "upgrade" not in args

    def classify(version, wheel, preflight):
        host.installed_authority(version, wheel)
        finish.require_active(preflight, (*host.SERVICES, host.AUXILIARY_UNITS[1]))
        assert not old.exists()
        return "installed_managed"

    monkeypatch.setattr(host, "classify", classify)
    return finish, target, old, pf, run, events, states


def test_committed_finishes_forward_under_one_lock(committed):
    finish, target, old, pf, run, events, states = committed
    finish.execute(target, pf, run=run)
    assert finish.update.state_files.read() is None
    assert finish.update.active_version() == target.version
    assert not old.exists()
    assert all(
        v["ActiveState"] == "active"
        for u, v in states.items()
        if u != finish.host.AUXILIARY_UNITS[0]
    )
    assert sum(e[-1] == "check" for e in events) == 1
    assert events[-1] == ("/usr/bin/systemctl", "start", finish.host.AUXILIARY_UNITS[1])


@pytest.mark.parametrize(
    "boundary",
    [
        "asset_before",
        "asset_after",
        "link_before",
        "link_after",
        "link_sync",
        "asset_sync",
        "retirement",
        "timer_enable",
        "lock_release",
        "timer_start",
        "state_before",
        "state_after",
    ],
)
def test_hard_interruption_exact_target_rerun(committed, monkeypatch, boundary):
    finish, target, old, pf, run, events, states = committed
    assets = finish.assets
    replace_path, sync, rmtree = os.replace, assets.sync, finish.shutil.rmtree
    remove = finish.update.state_files.remove
    mutation_lock = finish.transaction.recovery.mutation_lock
    fired = False

    def interrupt():
        nonlocal fired
        if not fired:
            fired = True
            raise Killed

    @contextmanager
    def released():
        with mutation_lock() as fd:
            yield fd
        if boundary == "lock_release":
            interrupt()

    def replace_file(source, destination):
        kind = "link" if destination == finish.host.ROOT / "venv" else "asset"
        if boundary == kind + "_before":
            interrupt()
        replace_path(source, destination)
        if boundary == kind + "_after":
            interrupt()

    def synced(directory):
        sync(directory)
        if boundary == "link_sync" and directory == finish.host.ROOT:
            interrupt()
        if boundary == "asset_sync" and directory != finish.host.ROOT:
            interrupt()

    def retire(path):
        if boundary == "retirement" and not fired:
            next(path.glob("*.whl")).unlink()
            # Interrupted recursive deletion can leave a dangling internal link.
            (path / "venv/dangling").symlink_to(path / "venv/deleted")
            interrupt()
        rmtree(path)

    def removed():
        if boundary == "state_before":
            interrupt()
        remove()
        if boundary == "state_after":
            interrupt()

    def interrupted(args, **kw):
        result = run(args, **kw)
        if boundary == "timer_enable" and args == (
            "/usr/bin/systemctl",
            "enable",
            finish.host.AUXILIARY_UNITS[1],
        ):
            interrupt()
        if boundary == "timer_start" and args == (
            "/usr/bin/systemctl",
            "start",
            finish.host.AUXILIARY_UNITS[1],
        ):
            interrupt()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(finish.transaction.recovery, "mutation_lock", released)
        patch.setattr(os, "replace", replace_file)
        patch.setattr(assets, "sync", synced)
        patch.setattr(finish.shutil, "rmtree", retire)
        patch.setattr(finish.update.state_files, "remove", removed)
        with pytest.raises(Killed):
            finish.execute(target, pf, run=interrupted)
    if boundary != "state_after":
        assert finish.update.state_files.read().phase == "committed"
        events.clear()
        finish.execute(target, pf, run=run)
        assert not any(e[-1] == "upgrade" for e in events)
    assert finish.update.state_files.read() is None
    assert finish.host.classify(target.version, target.wheel, pf) == "installed_managed"


@pytest.mark.parametrize(
    "failure",
    [
        "daemon-reload",
        "tmpfiles",
        "service",
        "doctor",
        "timer-enable",
        "timer-start",
        "state-remove",
    ],
)
def test_handled_failure_retires_every_unit_and_preserves_rerun(
    committed, monkeypatch, failure
):
    finish, target, old, pf, run, events, states = committed
    timer = finish.host.AUXILIARY_UNITS[1]

    def failing(args, **kw):
        result = run(args, **kw)
        if (
            (failure == "daemon-reload" and args[-1] == "daemon-reload")
            or (failure == "tmpfiles" and args[0] == "/usr/bin/systemd-tmpfiles")
            or (
                failure == "service"
                and args == ("/usr/bin/systemctl", "start", finish.host.SERVICES[1])
            )
            or (
                failure == "timer-enable"
                and args == ("/usr/bin/systemctl", "enable", timer)
            )
            or (
                failure == "timer-start"
                and args == ("/usr/bin/systemctl", "start", timer)
            )
        ):
            raise OSError("injected")
        if failure == "doctor" and args[-1] == "--json":
            return b'{"exit_code":0,"checks":[]}'
        return result

    remove = finish.update.state_files.remove

    def bad_remove():
        remove()
        raise OSError("fsync after unlink")

    with monkeypatch.context() as patch:
        if failure == "state-remove":
            patch.setattr(finish.update.state_files, "remove", bad_remove)
        with pytest.raises((OSError, finish.InstallError)):
            finish.execute(target, pf, run=failing)
    assert finish.update.state_files.read().phase == "committed"
    assert all(v["ActiveState"] == "inactive" for v in states.values())
    assert all(v["UnitFileState"] in {"disabled", "static"} for v in states.values())
    finish.execute(target, pf, run=run)
    assert finish.update.state_files.read() is None


@pytest.mark.parametrize(
    "damage",
    [
        "asset",
        "scratch_bytes",
        "scratch_mode",
        "scratch_hardlink",
        "scratch_symlink",
        "link_scratch",
        "extra_release",
        "manifest",
        "old_foreign",
    ],
)
def test_foreign_committed_authority_refused_without_commands(committed, damage):
    finish, target, old, pf, run, events, _ = committed
    asset = next(iter(finish.host.asset_bytes(target.wheel)))
    pending = finish.assets.scratch(asset)
    if damage == "asset":
        asset.write_bytes(b"foreign")
    elif damage.startswith("scratch"):
        if damage == "scratch_symlink":
            pending.symlink_to(asset)
        elif damage == "scratch_hardlink":
            pending.hardlink_to(asset)
        else:
            pending.write_bytes(
                b"foreign" if damage == "scratch_bytes" else b"target-unit"
            )
            if damage == "scratch_mode":
                pending.chmod(0o600)
    elif damage == "link_scratch":
        finish.assets.scratch(finish.host.ROOT / "venv").symlink_to(old / "venv")
    elif damage == "extra_release":
        (finish.host.RELEASES / "99").mkdir()
    elif damage == "manifest":
        target = replace(target, manifest_sha256="f" * 64)
    elif damage == "old_foreign":
        (old / "foreign").write_bytes(b"foreign")
    with pytest.raises((OSError, finish.InstallError)):
        finish.execute(target, pf, run=run)
    assert not events


@pytest.mark.parametrize(
    "boundary", ["create_before", "create_after", "file_fsync", "parent_fsync"]
)
def test_asset_write_interruption_preserves_active_bytes(
    committed, monkeypatch, boundary
):
    finish, target, _, _, _, _, _ = committed
    asset = next(iter(finish.host.asset_bytes(target.wheel)))
    pending = finish.assets.scratch(asset)
    open_file, fsync = os.open, os.fsync

    def opened(path, flags, *args, **kw):
        if path == pending and boundary == "create_before":
            raise Killed
        fd = open_file(path, flags, *args, **kw)
        if path == pending and boundary == "create_after":
            os.close(fd)
            raise Killed
        return fd

    def synced(fd):
        import stat

        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        fsync(fd)
        if (boundary == "file_fsync" and not directory) or (
            boundary == "parent_fsync" and directory
        ):
            raise Killed

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", opened)
        patch.setattr(os, "fsync", synced)
        with pytest.raises(Killed):
            finish.assets.replace_file(asset, b"target-unit", b"old-unit")
    assert asset.read_bytes() == (
        b"target-unit" if boundary == "parent_fsync" else b"old-unit"
    )
    if boundary == "create_after":
        # Incomplete bytes are not repair authority under the frozen exact rule.
        with pytest.raises(finish.InstallError):
            finish.assets.replace_file(asset, b"target-unit", b"old-unit")
        assert pending.read_bytes() == b""
    else:
        finish.assets.replace_file(asset, b"target-unit", b"old-unit")
        assert asset.read_bytes() == b"target-unit" and not pending.exists()


def test_reused_scratch_is_fsynced_before_replace(committed, monkeypatch):
    finish, target, _, _, _, _, _ = committed
    asset = next(iter(finish.host.asset_bytes(target.wheel)))
    pending = finish.assets.scratch(asset)
    pending.write_bytes(b"target-unit")
    inode = pending.stat().st_ino
    events = []
    fsync, replace_path = os.fsync, os.replace

    def synced(fd):
        if os.fstat(fd).st_ino == inode:
            events.append("file-sync")
        fsync(fd)

    def replaced(source, destination):
        events.append("replace")
        replace_path(source, destination)

    monkeypatch.setattr(os, "fsync", synced)
    monkeypatch.setattr(os, "replace", replaced)
    finish.assets.replace_file(asset, b"target-unit", b"old-unit")
    assert events == ["file-sync", "replace"]


def test_converged_rerun_persists_asset_and_release_parents(committed, monkeypatch):
    finish, target, _, pf, run, _, _ = committed
    state = finish.update.state_files.read()
    finish.execute(target, pf, run=run)
    # Simulate committed authority surviving final unlink on reboot.
    finish.update.state_files.write(state)
    synced = []
    sync = finish.assets.sync

    def persist(directory):
        synced.append(directory)
        sync(directory)

    monkeypatch.setattr(finish.assets, "sync", persist)
    finish.execute(target, pf, run=run)
    expected = {p.parent for p in finish.host.asset_bytes(target.wheel)}
    assert expected | {finish.host.ROOT, finish.host.RELEASES} <= set(synced)


def test_phase_recovery_failure_still_retires_all_services(committed, monkeypatch):
    finish, target, _, pf, run, _, states = committed
    remove = finish.update.state_files.remove

    def failed_remove():
        remove()
        raise OSError("primary unlink fsync failure")

    def failed_write(state):
        raise OSError("secondary state recovery failure")

    monkeypatch.setattr(finish.update.state_files, "remove", failed_remove)
    monkeypatch.setattr(finish.update.state_files, "write", failed_write)
    with pytest.raises(OSError, match="primary unlink fsync failure") as caught:
        finish.execute(target, pf, run=run)
    assert "secondary state recovery" in str(caught.value.__cause__)
    assert all(v["ActiveState"] == "inactive" for v in states.values())
    assert all(v["UnitFileState"] in {"disabled", "static"} for v in states.values())
