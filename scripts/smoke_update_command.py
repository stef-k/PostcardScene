"""Invoke extracted install.py's public CLI with bounded x86 graphics substitution.

Test-only observers execute real commands, assert ordering/lock authority, and can
SIGKILL this controller at three durable boundaries. No bundle bytes are edited.
"""

import fcntl
import importlib.util
import json
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

LOCK = Path("/var/lib/postcardscene-web/backup.lock")
GRAPHICS = "postcardscene-graphics.service"


def lock_owned(expected):
    with LOCK.open("rb") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            assert expected, "Timer started before update lock release"
        else:
            assert not expected, "Continuous shared mutation lock lost"


def interrupt(boundary, selected):
    if boundary == selected:
        print("SIGKILL at " + boundary, flush=True)
        os.kill(os.getpid(), signal.SIGKILL)


def instrument(host, preflight, selected):
    transaction = sys.modules["postcardscene_install_update_transaction"]
    recovery = sys.modules["postcardscene_install_update_recovery"]
    assets = sys.modules["postcardscene_install_update_assets"]
    phases = sys.modules["postcardscene_install_update_state"]
    command = host.command
    service_state = host.service_state
    graphics_active = False
    events = []
    created = None
    quiescence = [
        ("stop", host.AUXILIARY_UNITS[1]),
        ("disable", host.AUXILIARY_UNITS[1]),
        ("stop", host.AUXILIARY_UNITS[0]),
        *[(a, u) for u in reversed(host.SERVICES) for a in ("stop", "disable")],
    ]

    def state(observer, unit):
        try:
            result = service_state(observer, unit)
        except host.InstallError:
            print(
                "Unavailable unit state:",
                unit,
                observer.Host().command(
                    (
                        "/usr/bin/systemctl",
                        "show",
                        unit,
                        "--property=LoadState,ActiveState,UnitFileState",
                    )
                ),
                flush=True,
            )
            raise
        if unit == GRAPHICS and graphics_active:
            result["ActiveState"] = "active"
        return result

    def run(args, **options):
        nonlocal graphics_active, created
        if args[0] == "/usr/bin/systemctl" and len(args) >= 3:
            action, unit = args[1:3]
            if action in {"stop", "disable", "enable", "start"}:
                lock_owned(not (action == "start" and unit == host.AUXILIARY_UNITS[1]))
                events.append((action, unit))
            if unit == GRAPHICS and action == "stop":
                graphics_active = False
            if unit == GRAPHICS and action == "start":
                graphics_active = True
                return None
        if (
            len(args) > 4
            and args[3] == "-c"
            and args[4] in (recovery.CREATE, recovery.VERIFY)
        ):
            lock_owned(True)
            assert args[0] == "/opt/postcardscene/releases/0.0.0/venv/bin/python"
            assert options["user"] == "postcardscene-web"
            fd = options["pass_fds"][0]
            assert os.path.samestat(os.fstat(fd), LOCK.stat())
            data = command(args, **options)
            if args[4] == recovery.CREATE:
                created = json.loads(data)
            else:
                assert json.loads(data) == created
                assert created["application_version"] == "0.0.0"
                print(
                    "Fresh old-version recovery strictly reverified under inherited lock",
                    flush=True,
                )
            return data
        if args[-2:] == ("db", "upgrade"):
            lock_owned(True)
            assert events[-len(quiescence) :] == quiescence
            assert phases.read().phase == "migrating"
            assert args[:6] == (
                str(host.RELEASES / phases.read().to_version / "venv/bin/python"),
                "-I",
                "-m",
                "flask",
                "--app",
                "postcardscene.web:create_app",
            )
            assert options["user"] == "postcardscene-web"
            interrupt("migrating", selected)
            print("Exact staged-target db upgrade + check under quiescence", flush=True)
        if args[-2:] == ("db", "check"):
            lock_owned(True)
        if args[0].endswith("/postcardscene-doctor"):
            lock_owned(True)
            data = json.loads(command(args, **options))
            # Real installed doctor checks every other authority. Only the absent
            # physical graphics service's activation observation is substituted.
            assert graphics_active
            for check in data["checks"]:
                if check["identifier"] == "service_graphics":
                    assert check["state"] == "degraded", check
                    check["state"] = "ready"
            return json.dumps(data)
        result = command(args, **options)
        if args == ("/usr/bin/systemctl", "start", "postcardscene-runtime.service"):
            # Runtime Wants=graphics can start it indirectly despite the explicit
            # start substitution. Stop that unsupported display launch on this VM;
            # the weak dependency leaves the real runtime service running.
            command(("/usr/bin/systemctl", "stop", GRAPHICS))
        return result

    host.service_state = state
    host.command = run
    preflight.preflight = lambda **kw: SimpleNamespace(
        ok=True, plan=SimpleNamespace(python="/usr/bin/python3", tools=())
    )
    original_stage = host.stage_payload

    def stage(*args, **kwargs):
        original = kwargs["on_created"]

        def created_directory(info):
            original(info)
            interrupt("staging", selected)

        kwargs["on_created"] = created_directory
        return original_stage(*args, **kwargs)

    host.stage_payload = stage
    original_write = phases.write

    def write(value):
        lock_owned(True)
        original_write(value)
        if value.phase == "prepared":
            interrupt("prepared", selected)

    phases.write = write
    original_link = assets.replace_link

    def link(value):
        lock_owned(True)
        if selected == "committed":
            assert value.phase == "committed"
            for unit in (*host.SERVICES, host.AUXILIARY_UNITS[1]):
                assert state(preflight, unit)["UnitFileState"] == "disabled"
            assets.scratch(host.ROOT / "venv").symlink_to(
                host.RELEASES / value.to_version / "venv"
            )
            interrupt("committed", selected)
        return original_link(value)

    assets.replace_link = link
    # Keep the exact production quiescence proof, including live UID processes.
    assert transaction.quiesce.__module__ == "postcardscene_install_update_transaction"


def main():
    bundle, destination, selected = sys.argv[1:]
    spec = importlib.util.spec_from_file_location(
        "target_installer", Path(bundle) / "install.py"
    )
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    original = entry.load_support

    def load(path):
        inputs, host, preflight, members = original(path)
        instrument(host, preflight, selected)
        return inputs, host, preflight, members

    entry.load_support = load
    sys.argv = [
        str(Path(bundle) / "install.py"),
        "update",
        "--backup-destination",
        destination,
    ]
    raise SystemExit(entry.main())


if __name__ == "__main__":
    main()
