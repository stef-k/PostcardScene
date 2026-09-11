"""Internal recovery-locked forward transaction, ending at durable committed."""

import json
from contextlib import contextmanager
from dataclasses import replace

import postcardscene_install_host as host
import postcardscene_install_update as update
import postcardscene_install_update_database as database
import postcardscene_install_update_recovery as recovery
from postcardscene_install_services import InstallError


def quiesce(preflight, run):
    """Retire every unit even after failure; never infer this from a phase file."""
    failed = False
    timer, oneshot = host.AUXILIARY_UNITS[1], host.AUXILIARY_UNITS[0]
    actions = [("stop", timer), ("disable", timer), ("stop", oneshot)]
    actions.extend(
        (action, unit)
        for unit in reversed(host.SERVICES)
        for action in ("stop", "disable")
    )
    for action, unit in actions:
        try:
            run(("/usr/bin/systemctl", action, unit), timeout=45)
        except (Exception, KeyboardInterrupt):
            failed = True
    for unit in (*host.AUXILIARY_UNITS, *host.SERVICES):
        try:
            values = host.service_state(preflight, unit)
            expected = "static" if unit == oneshot else "disabled"
            if (
                values.get("LoadState") != "loaded"
                or values.get("ActiveState") != "inactive"
                or values.get("UnitFileState") != expected
            ):
                failed = True
        except (Exception, KeyboardInterrupt):
            failed = True
    try:
        host.require_no_processes(host.preserved_identities())
    except (Exception, KeyboardInterrupt):
        failed = True
    if failed:
        raise InstallError("update_quiescence_failed")


def target_database(target, run, lock_fd, *, upgrade):
    python = str(host.RELEASES / target.version / "venv/bin/python")
    cli = (python, "-I", "-m", "flask", "--app", "postcardscene.web:create_app", "db")
    if upgrade:
        run(
            (*cli, "upgrade"),
            user="postcardscene-web",
            timeout=300,
            pass_fds=(lock_fd,),
        )
    data = run(
        (*cli, "check"),
        user="postcardscene-web",
        timeout=300,
        capture=True,
        pass_fds=(lock_fd,),
    )
    try:
        checked = json.loads(data, object_pairs_hook=update.inputs.unique_object)
    except (ValueError, TypeError) as error:
        raise InstallError("update_database_check_failed") from error
    if checked != {
        "application": "postcardscene",
        "application_version": target.version,
        "sqlite_application_id": target.identity["application_id"],
        "schema_revision": target.identity["schema"],
    }:
        raise InstallError("update_database_check_failed")


def prepared_state(current, target):
    return update.state_files.UpdateState(
        "prepared",
        current["version"],
        current["wheel_sha256"],
        current["schema"],
        target.version,
        target.identity["wheel_sha256"],
        target.manifest_sha256,
        target.identity["schema"],
    )


def commit(current, target, state, preflight, point, run, destination, archive):
    lock_fd = point.lock_fd
    if state is None or state.phase == "prepared":
        point.select(destination, archive)
        if state is None:
            state = prepared_state(current, target)
            update.state_files.write(state)
        quiesce(preflight, run)
    else:
        quiesce(preflight, run)
        head = database.classify(state, target.identity["application_id"])
        if state.phase == "committed":
            # Commit cannot be undone, even if an operator restored an old DB.
            target_database(target, run, lock_fd, upgrade=False)
            return state
        if state.from_schema != state.to_schema and head == "target":
            target_database(target, run, lock_fd, upgrade=False)
            state = replace(state, phase="committed")
            update.state_files.write(state)
            return state
        # Old head (including operator same-version restore) gets a fresh gate.
        # Same-schema reruns still execute the exact target upgrade/check pair.
        point.select(destination, archive)
        quiesce(preflight, run)
    if database.classify(state, target.identity["application_id"]) != "old":
        raise InstallError("update_database_unrecognized")
    state = replace(state, phase="migrating")
    update.state_files.write(state)
    target_database(target, run, lock_fd, upgrade=True)
    state = replace(state, phase="committed")
    update.state_files.write(state)
    return state


@contextmanager
def transaction(target, preflight, *, destination=None, archive=None, run=None):
    """Stage then commit, retaining the same flock across the caller's context.

    #177 may finish forward inside this context. Exiting never activates services,
    rolls back data, removes phase authority or deletes old release bytes.
    """
    if destination is not None and archive is not None:
        raise InstallError("recovery_selection_conflict")
    run = run or host.command
    current, target, initial = update.prepare_target(target, preflight, run)
    try:
        with recovery.mutation_lock() as lock_fd:
            # A competing attempt could have committed while we staged. Its phase
            # is authoritative; never overwrite it with stale prepared state.
            state = update.state_files.read()
            if state is not None:
                update.require_state_target(state, current, target)
            point = recovery.Recovery(current, lock_fd, run)
            committed = commit(
                current, target, state, preflight, point, run, destination, archive
            )
            yield committed
    except (Exception, KeyboardInterrupt) as error:
        if initial is None:
            try:
                if update.state_files.read() is None:
                    update.discard_target(target, current["version"])
            except (OSError, InstallError) as cleanup:
                error.add_note(
                    "Staged target cleanup uncertain; preserve for inspection."
                )
                raise error from cleanup
        raise
