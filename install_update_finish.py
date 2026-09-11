"""Committed update authority, target activation and final retirement."""

import json
import os
import shutil
import stat

import postcardscene_install_host as host
import postcardscene_install_update as update
import postcardscene_install_update_assets as assets
import postcardscene_install_update_transaction as transaction
from postcardscene_install_services import InstallError

CRITICAL = frozenset(
    {
        "release",
        "config_authority",
        "installed_assets",
        "conflict_record",
        "identities",
        "durable_permissions",
        "database_permissions",
        "private_key_permissions",
        "service_graphics",
        "service_runtime",
        "service_web",
        "database",
        "web_config",
    }
)


def recognize(target, preflight):
    state = update.state_files.read()
    if state is None or state.phase != "committed":
        raise InstallError("update_committed_required")
    current = dict(
        version=state.from_version,
        wheel_sha256=state.from_wheel_sha256,
        schema=state.from_schema,
    )
    update.require_state_target(state, current, target)
    runtime, _, shared, _ = host.preserved_authority()
    host.metadata(host.RELEASES, mode=0o755, kind=stat.S_ISDIR)
    identity, wheel = update.stored_release(target.version)
    if identity != target.identity or wheel != target.wheel:
        raise InstallError("update_target_changed")
    old = host.RELEASES / state.from_version
    releases = {host.RELEASES / target.version}
    if os.path.lexists(old):
        host.metadata(old, mode=0o755, kind=stat.S_ISDIR)
        releases.add(old)
    if set(host.RELEASES.iterdir()) != releases:
        raise InstallError("update_release_unknown")
    target_assets = host.asset_bytes(target.wheel)
    coherent = assets.inspect_link(state)
    # Old bytes are needed only for unfinished asset convergence. Once every
    # asset and link is target-exact, interrupted retirement needs no old wheel.
    all_target = all(host.read_regular(p) == b for p, b in target_assets.items())
    old_assets = {}
    if coherent and all_target and os.path.lexists(old):
        host.validate_tree(old, 0, 0, payload=True, allow_missing_links=True)
    if not (coherent and all_target):
        old_identity, old_wheel = update.stored_release(state.from_version)
        update.require_state_target(state, old_identity, target)
        old_assets = host.asset_bytes(old_wheel)
    for path, content in target_assets.items():
        assets.inspect_file(path, content, old_assets.get(path))
    for directory in host.DROPINS:
        host.metadata(directory, mode=0o755, kind=stat.S_ISDIR)
        path = directory / "permissions.conf"
        allowed = {path}
        if os.path.lexists(assets.scratch(path)):
            allowed.add(assets.scratch(path))
        if set(directory.iterdir()) != allowed:
            raise InstallError("update_asset_unknown")
    allowed = {host.MARKER, host.RELEASES, host.ROOT / "venv", update.state_files.STATE}
    for path in (assets.scratch(host.ROOT / "venv"), update.state_files.SCRATCH):
        if os.path.lexists(path):
            allowed.add(path)
    if os.path.lexists(update.state_files.SCRATCH):
        host.metadata(update.state_files.SCRATCH, mode=0o600)
    if set(host.ROOT.iterdir()) != allowed:
        raise InstallError("update_root_unknown")
    host.metadata(host.CACHE, runtime, shared, 0o700, stat.S_ISDIR)
    for path, mode in zip(host.TRANSIENTS, (0o750, 0o700), strict=True):
        if os.path.lexists(path):
            host.metadata(path, runtime, shared, mode, stat.S_ISDIR)
    host.conflict_record(preflight)
    host.service_authority(preflight)
    return state, coherent and all_target


def require_active(preflight, units):
    for unit in units:
        if host.service_state(preflight, unit) != {
            "LoadState": "loaded",
            "ActiveState": "active",
            "UnitFileState": "enabled",
        }:
            raise InstallError("update_activation_failed")


def doctor(run):
    data = run(
        (str(host.ROOT / "venv/bin/postcardscene-doctor"), "--json"),
        capture=True,
        timeout=300,
        accepted_statuses=(0, 1),
    )
    try:
        report = json.loads(data, object_pairs_hook=update.inputs.unique_object)
        checks = report["checks"]
        if report["exit_code"] not in (0, 1) or not isinstance(checks, list):
            raise ValueError
        states = {c["identifier"]: c["state"] for c in checks}
        if len(states) != len(checks) or "fatal" in states.values():
            raise ValueError
        if any(states.get(name) != "ready" for name in CRITICAL):
            raise ValueError
    except (ValueError, TypeError, KeyError) as error:
        raise InstallError("update_doctor_failed") from error


def retire_old(state, target, preflight):
    actual, coherent = recognize(target, preflight)
    if actual != state or not coherent:
        raise InstallError("update_retirement_authority_invalid")
    old = host.RELEASES / state.from_version
    if os.path.lexists(old):
        host.validate_tree(old, 0, 0, payload=True, allow_missing_links=True)
        shutil.rmtree(old)
    assets.sync(host.RELEASES)


def finish_locked(state, target, preflight, run):
    actual, coherent = recognize(target, preflight)
    if actual != state:
        raise InstallError("update_state_identity_mismatch")
    old_assets = {}
    if not coherent:
        transaction.quiesce(preflight, run)
        _, old_wheel = update.stored_release(state.from_version)
        old_assets = host.asset_bytes(old_wheel)
    for path, content in host.asset_bytes(target.wheel).items():
        assets.replace_file(path, content, old_assets.get(path))
    if not coherent:
        run(("/usr/bin/systemctl", "daemon-reload"))
        run(("/usr/bin/systemd-tmpfiles", "--create", str(host.TMPFILES)))
    assets.replace_link(state)
    recognize(target, preflight)
    for unit in host.SERVICES:
        if host.service_state(preflight, unit).get("UnitFileState") != "enabled":
            run(("/usr/bin/systemctl", "enable", unit))
    for unit in host.SERVICES:
        if host.service_state(preflight, unit).get("ActiveState") != "active":
            run(("/usr/bin/systemctl", "start", unit))
        require_active(preflight, (unit,))
    doctor(run)
    retire_old(state, target, preflight)
    run(("/usr/bin/systemctl", "enable", host.AUXILIARY_UNITS[1]))


def execute(target, preflight, *, destination=None, archive=None, run=None):
    run = run or host.command
    committed = None
    try:
        with transaction.transaction(
            target, preflight, destination=destination, archive=archive, run=run
        ) as state:
            finish_locked(state, target, preflight, run)
            committed = state
        timer = host.AUXILIARY_UNITS[1]
        run(("/usr/bin/systemctl", "start", timer))
        require_active(preflight, (*host.SERVICES, timer))
        update.state_files.clean_scratch()
        update.state_files.remove()
        if (
            host.classify(target.version, target.wheel, preflight)
            != "installed_managed"
        ):
            raise InstallError("update_final_authority_invalid")
    except (Exception, KeyboardInterrupt) as error:
        if committed is not None:
            cleanup = None
            try:
                if update.state_files.read() is None:
                    update.state_files.write(committed)
            except (Exception, KeyboardInterrupt) as failure:
                cleanup = failure
            try:
                transaction.quiesce(preflight, run)
            except (Exception, KeyboardInterrupt) as failure:
                cleanup = failure
            if cleanup is not None:
                error.add_note(
                    "Update retirement or phase recovery uncertain; preserve target authority."
                )
                raise error from cleanup
        raise
