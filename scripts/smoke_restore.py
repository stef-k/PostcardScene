"""Installed restore CLI with real root/DAC/systemd; graphics alone is simulated."""

import json
import pwd
import subprocess
from pathlib import Path

PYTHON = "/opt/postcardscene/venv/bin/python"
DATABASE = Path("/var/lib/postcardscene/postcardscene.sqlite3")
KEY = Path("/var/lib/postcardscene-web/session.key")

# Invoke the installed CLI, substituting only absent physical graphics activation.
# All unit/file authority, runtime/web services, timer and root lock remain real.
RESTORE = """
import sys
from postcardscene.backup import cli, restore_host as host
original = host.systemctl
active = False
def systemctl(*args):
    global active
    if args == ("stop", host.SERVICES[0]):
        active = False
    if args == ("start", host.SERVICES[0]):
        active = True
        return ""
    result = original(*args)
    if active and args == ("show", host.SERVICES[0], "--property=LoadState,ActiveState,UnitFileState"):
        return result.replace("ActiveState=inactive", "ActiveState=active")
    return result
host.systemctl = systemctl
raise SystemExit(cli.main(["restore", sys.argv[1]]))
"""


def restore_archive(archive):
    completed = subprocess.run(
        (PYTHON, "-I", "-B", "-c", RESTORE, str(archive)),
        capture_output=True,
        timeout=180,
    )
    assert completed.returncode == 0, "Installed restore CLI failed"
    assert json.loads(completed.stdout) == {"result": "restore_complete"}
    assert DATABASE.stat().st_uid == pwd.getpwnam("postcardscene").pw_uid
    assert KEY.stat().st_uid == pwd.getpwnam("postcardscene-web").pw_uid
    subprocess.run(
        (
            "/usr/bin/systemctl",
            "is-active",
            "--quiet",
            "postcardscene-backup.timer",
            "postcardscene-runtime.service",
            "postcardscene-web.service",
        ),
        check=True,
    )
