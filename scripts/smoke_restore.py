"""Installed restore CLI with real root/DAC/systemd; graphics alone is simulated."""

import json
import os
import pwd
import subprocess
import tempfile
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


ARCHIVE_FIXTURE = """
import hashlib
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from postcardscene.backup.archive import build
root = Path(sys.argv[1])
with sqlite3.connect("/var/lib/postcardscene/postcardscene.sqlite3") as source:
    with sqlite3.connect(root / "postcardscene.sqlite3") as target:
        source.backup(target)
for source in ("/etc/postcardscene/config.py", "/var/lib/postcardscene-web/session.key"):
    shutil.copyfile(source, root / Path(source).name)
archive = build(root, datetime.now(timezone.utc), version("postcardscene"), lambda: None)
archive.with_name(archive.name + ".sha256").write_text(
    hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\\n"
)
print(archive.name)
"""


def restore_smoke():
    web = pwd.getpwnam("postcardscene-web")
    runtime = pwd.getpwnam("postcardscene")
    with tempfile.TemporaryDirectory(
        prefix="postcardscene-restore-smoke-", dir="/tmp"
    ) as temp:
        destination = Path(temp)
        os.chown(destination, web.pw_uid, web.pw_gid)
        # Build an earlier archive fixture without manual/scheduled mutation on
        # this fresh host. SQLite's backup API supplies a consistent source DB.
        result = subprocess.run(
            (PYTHON, "-I", "-B", "-c", ARCHIVE_FIXTURE, temp),
            capture_output=True,
            timeout=180,
            check=True,
        )
        archive = destination / result.stdout.decode().strip()
        assert not (KEY.parent / "backup.lock").exists()
        config = Path("/etc/postcardscene/config.py")
        marker = Path("/opt/postcardscene/service-conflicts.json")
        preserved = {path: path.read_bytes() for path in (config, marker, KEY)}
        # First operation replaces a fresh matching bootstrap installation. Then
        # corrupt current bytes prove the same command repairs an in-place host.
        for corrupt in (False, True):
            if corrupt:
                for path in (DATABASE, config, KEY):
                    path.write_bytes(b"damaged current content")
            completed = subprocess.run(
                (PYTHON, "-I", "-B", "-c", RESTORE, str(archive)),
                capture_output=True,
                timeout=180,
            )
            assert completed.returncode == 0, (
                "Installed restore CLI failed: " + completed.stdout.decode()
            )
            assert json.loads(completed.stdout) == {"result": "restore_complete"}
            for path, value in preserved.items():
                assert path.read_bytes() == value
            assert DATABASE.stat().st_uid == runtime.pw_uid
            assert KEY.stat().st_uid == web.pw_uid
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
            subprocess.run(
                (
                    "/usr/bin/systemctl",
                    "stop",
                    "postcardscene-backup.timer",
                    "postcardscene-backup.service",
                    "postcardscene-web.service",
                    "postcardscene-runtime.service",
                ),
                check=True,
            )
    print(
        "Installed root restore passed for fresh matching bootstrap and corrupt in-place state; physical graphics activation simulated."
    )
