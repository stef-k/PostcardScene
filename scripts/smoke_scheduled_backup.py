"""Disposable installed-Linux scheduled backup and auxiliary recovery evidence."""

import json
import os
import pwd
import subprocess
import tempfile
from pathlib import Path

PYTHON = "/opt/postcardscene/venv/bin/python"
SERVICE = "postcardscene-backup.service"
TIMER = "postcardscene-backup.timer"


def web_python(code, *args):
    web = pwd.getpwnam("postcardscene-web")
    result = subprocess.run(
        (PYTHON, "-I", "-B", "-c", code, *args),
        user=web.pw_uid,
        group=web.pw_gid,
        extra_groups=[],
        umask=0o077,
        cwd="/",
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, "Installed backup policy operation failed"
    return result.stdout


def policy(destination, enabled):
    web_python(
        "import sys; from postcardscene.persistence import Database; "
        "from postcardscene.backup_policy import replace_backup_policy; "
        "replace_backup_policy(Database(), sys.argv[2] == 'yes', sys.argv[1], 0, 1)",
        str(destination),
        "yes" if enabled else "no",
    )


def history():
    return web_python(
        "import json; from dataclasses import asdict; "
        "from postcardscene.persistence import Database; "
        "from postcardscene.backup_policy import get_backup_policy; "
        "p,h=get_backup_policy(Database()); print(json.dumps(asdict(h),sort_keys=True))"
    )


def recovery(entrypoint, host, preflight, destination):
    """Exercise real timer retirement after coherent activation and a later failure."""
    before = {p.name: p.read_bytes() for p in destination.iterdir()}
    # Disabled policy must never create another archive, including persistent wakes.
    for phase in ("activation", "complete"):
        host.command(("/usr/bin/systemctl", "enable", TIMER))
        host.command(("/usr/bin/systemctl", "start", TIMER))
        host.command(("/usr/bin/systemctl", "start", SERVICE))
        operation = entrypoint.Installation(Path("/unused"), host.command)
        operation.host = host
        operation.durable = operation.activation = True
        operation.phase = phase
        operation.recover()
        host.require_auxiliary_stopped(preflight)
        assert before == {p.name: p.read_bytes() for p in destination.iterdir()}
    host.command(("/usr/bin/systemctl", "enable", TIMER))
    host.command(("/usr/bin/systemctl", "start", TIMER))


def scheduled_lifecycle(entrypoint, host, preflight, lifecycle):
    with tempfile.TemporaryDirectory(
        prefix="postcardscene-scheduled-", dir="/tmp"
    ) as temp:
        destination = Path(temp)
        web = pwd.getpwnam("postcardscene-web")
        os.chown(destination, web.pw_uid, web.pw_gid)
        host.command(("/usr/bin/systemctl", "enable", TIMER))
        host.command(("/usr/bin/systemctl", "start", TIMER))
        host.command(("/usr/bin/systemctl", "start", SERVICE))
        host.require_auxiliary_installed(preflight)
        assert not list(destination.iterdir())
        policy(destination, True)
        host.command(("/usr/bin/systemctl", "start", SERVICE))
        captured = history()
        assert json.loads(captured)["last_result"] == "ready"
        before = {p.name: p.read_bytes() for p in destination.iterdir()}
        assert len(before) == 2
        host.command(("/usr/bin/systemctl", "start", SERVICE))
        assert history() == captured
        assert before == {p.name: p.read_bytes() for p in destination.iterdir()}
        policy(destination, False)
        recovery(entrypoint, host, preflight, destination)
        lifecycle()  # Also compares the exact DB/policy/history bytes across reinstall.
        assert before == {p.name: p.read_bytes() for p in destination.iterdir()}
    print(
        "Real web-UID scheduled create/no-op and auxiliary recovery/removal preserved archive/history."
    )
