"""Read exact managed authority and take a bounded online SQLite snapshot."""

import ast
import grp
import os
import pwd
import sqlite3
import stat
import time
from pathlib import Path

from postcardscene.persistence import DEFAULT_DATABASE_PATH
from postcardscene.session_secret import DEFAULT_SECRET_PATH

from .archive import check_database
from .files import DB_NAME, MEMBER_LIMITS, BackupError, directory, private_file, regular

CONFIG = Path("/etc/postcardscene/config.py")
DATABASE = DEFAULT_DATABASE_PATH
KEY = DEFAULT_SECRET_PATH


def identities():
    return (
        pwd.getpwnam("postcardscene").pw_uid,
        pwd.getpwnam("postcardscene-web").pw_uid,
        grp.getgrnam("postcardscene").gr_gid,
        grp.getgrnam("postcardscene-web").gr_gid,
    )


def require_directory(fd, uid, gid, mode):
    info = os.fstat(fd)
    if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (uid, gid, mode):
        raise BackupError("managed_authority_invalid")


def read_managed(path, metadata, parent_metadata, limit):
    with directory(path.parent) as parent:
        require_directory(parent, *parent_metadata)
        with regular(parent, path.name, limit, metadata=metadata) as source:
            value = source.read(limit + 1)
            if len(value) > limit:
                raise BackupError("managed_authority_invalid")
            return value


def validate_config(value):
    """Match managed install's literal-only authority without executing config."""
    values = {}
    for node in ast.parse(value).body:
        if (
            not isinstance(node, ast.Assign)
            or len(node.targets) != 1
            or not isinstance(node.targets[0], ast.Name)
        ):
            raise BackupError("managed_authority_invalid")
        values[node.targets[0].id] = ast.literal_eval(node.value)
    if values.get("DATABASE_PATH", str(DATABASE)) != str(DATABASE) or values.get(
        "SESSION_SECRET_PATH", str(KEY)
    ) != str(KEY):
        raise BackupError("managed_authority_invalid")


def snapshot(
    target, runtime, web, shared, progress, *, cancelled=lambda: False, timeout=120.0
):
    deadline = time.monotonic() + timeout

    def advance(status, remaining, total):
        if cancelled() or time.monotonic() >= deadline:
            raise BackupError("capture_timeout")
        if total * page_size > MEMBER_LIMITS[DB_NAME]:
            raise BackupError("size_limit")
        if status == sqlite3.SQLITE_OK or status == sqlite3.SQLITE_DONE:
            progress()

    with directory(DATABASE.parent) as parent:
        require_directory(parent, runtime, shared, 0o2770)
        with regular(
            parent,
            DATABASE.name,
            MEMBER_LIMITS[DB_NAME],
            metadata=(runtime, shared, 0o660),
        ) as source_file:
            # SQLite owns opening WAL/SHM by canonical basename. Parents are pinned
            # and identities checked around open; managed service UIDs are trusted.
            for suffix in ("-wal", "-shm"):
                try:
                    with regular(
                        parent,
                        DATABASE.name + suffix,
                        MEMBER_LIMITS[DB_NAME],
                        allow_empty=True,
                    ) as sidecar:
                        info = os.fstat(sidecar.fileno())
                        if (
                            info.st_uid not in (runtime, web)
                            or info.st_gid != shared
                            or stat.S_IMODE(info.st_mode) != 0o660
                        ):
                            raise BackupError("managed_authority_invalid")
                except FileNotFoundError:
                    pass
            source = sqlite3.connect(
                DATABASE.as_uri() + "?mode=ro", uri=True, timeout=0.1
            )
            try:
                current = os.stat(DATABASE.name, dir_fd=parent, follow_symlinks=False)
                opened = os.fstat(source_file.fileno())
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    raise BackupError("managed_authority_invalid")
                with private_file(target):
                    pass
                destination = sqlite3.connect(target)
                try:
                    page_size = source.execute("PRAGMA page_size").fetchone()[0]
                    source.backup(destination, pages=256, progress=advance, sleep=0.05)
                finally:
                    destination.close()
            finally:
                source.close()
    if target.stat().st_size > MEMBER_LIMITS[DB_NAME]:
        raise BackupError("size_limit")
    check_database(target)


def capture(scratch, progress):
    runtime, web, shared, private = identities()
    if os.geteuid() != web:
        raise BackupError("web_identity_required")
    config = read_managed(CONFIG, (0, shared, 0o640), (0, shared, 0o750), 65536)
    validate_config(config)
    key = read_managed(KEY, (web, shared, 0o600), (web, private, 0o700), 32)
    if len(key) != 32:
        raise BackupError("managed_authority_invalid")
    for name, value in (("config.py", config), ("session.key", key)):
        with private_file(scratch / name) as target:
            target.write(value)
    snapshot(scratch / DB_NAME, runtime, web, shared, progress)
