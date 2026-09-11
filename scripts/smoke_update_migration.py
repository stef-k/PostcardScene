"""Real installed target migration against one separate recognized ancestor DB."""

import os
import pwd
import subprocess
import sys
import tempfile
from pathlib import Path

SEED = """
import sqlite3, sys
from alembic import command
from postcardscene.persistence import Database
from postcardscene.schema import migration_config
path = sys.argv[1]
db = Database(path, create=True)
with db.engine.begin() as connection:
    command.upgrade(migration_config(connection), "0011_display_power_settings")
db.engine.dispose()
with sqlite3.connect(path) as connection:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("UPDATE application_settings SET timezone='Europe/Athens'")
    assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0011_display_power_settings",)
    assert not connection.execute("SELECT name FROM sqlite_master WHERE name='backup_policy'").fetchall()
"""
CHECK = """
import sqlite3, sys
from postcardscene.persistence import Database
Database(sys.argv[1]).check()
with sqlite3.connect(sys.argv[1]) as connection:
    assert connection.execute("SELECT timezone FROM application_settings").fetchone() == ("Europe/Athens",)
    assert connection.execute("SELECT enabled, local_hour, retention_count FROM backup_policy").fetchone() == (0, 3, 7)
"""


def ancestor_migration(entry, bundle):
    _, host, preflight, members = entry.load_support(bundle)
    update = sys.modules["postcardscene_install_update"]
    transaction = sys.modules["postcardscene_install_update_transaction"]
    target = update.target_inputs(bundle, members)
    python = str(host.RELEASES / target.version / "venv/bin/python")
    web = pwd.getpwnam("postcardscene-web")
    with tempfile.TemporaryDirectory(
        prefix="postcardscene-ancestor-", dir="/tmp"
    ) as temp:
        root = Path(temp)
        os.chown(root, web.pw_uid, web.pw_gid)
        database = root / "fixture.sqlite3"
        config = root / "config.py"
        config.write_text(f"DATABASE_PATH = {str(database)!r}\n")
        config.chmod(0o644)
        calls = []

        def run(args, **options):
            # Only the config path differs: never downgrade or replace the live DB.
            assert options["user"] == "postcardscene-web"
            calls.append(args[-1])
            result = subprocess.run(
                args,
                user=web.pw_uid,
                group=web.pw_gid,
                extra_groups=[],
                umask=0o007,
                cwd="/",
                capture_output=True,
                timeout=300,
                pass_fds=options.get("pass_fds", ()),
                env={**host.ENV, "POSTCARDSCENE_CONFIG": str(config)},
            )
            assert result.returncode == 0, "Installed ancestor migration command failed"
            return result.stdout

        run((python, "-I", "-B", "-c", SEED, str(database)), user="postcardscene-web")
        with transaction.recovery.mutation_lock() as lock_fd:
            transaction.target_database(target, run, lock_fd, upgrade=True)
        assert calls[-2:] == ["upgrade", "check"]
        run((python, "-I", "-B", "-c", CHECK, str(database)), user="postcardscene-web")
    print(
        "Real installed target db upgrade/check: 0011 ancestor -> target, data preserved; separate fixture only",
        flush=True,
    )
