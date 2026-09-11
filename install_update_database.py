"""Read-only interrupted-head classification on a bounded private main/WAL copy."""

import os
import sqlite3
import stat
import tempfile
from pathlib import Path

import postcardscene_install_host as host
from postcardscene_install_services import InstallError

LIMIT = 4 * 1024**3


def signature(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise InstallError("update_database_unrecognized")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def copy_database(directory):
    # Services are quiescent and the mutation lock is held. Snapshot signatures
    # still reject changes; SQLite never opens the installed file or its sidecars.
    paths = (host.DATABASE, Path(str(host.DATABASE) + "-wal"))
    before = tuple(signature(path) for path in paths)
    if before[0] is None or sum(s[2] for s in before if s) > LIMIT:
        raise InstallError("update_database_unrecognized")
    for path, expected in zip(paths, before, strict=True):
        if expected is None:
            continue
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (info.st_dev, info.st_ino) != expected[:2]:
                raise InstallError("update_database_unrecognized")
            target = directory / path.name
            out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(out, "wb") as destination:
                remaining = expected[2]
                while remaining:
                    block = source.read(min(1024 * 1024, remaining))
                    if not block:
                        raise InstallError("update_database_unrecognized")
                    destination.write(block)
                    remaining -= len(block)
    if tuple(signature(path) for path in paths) != before:
        raise InstallError("update_database_unrecognized")
    return directory / host.DATABASE.name


def classify(state, application_id):
    try:
        with tempfile.TemporaryDirectory(
            prefix="postcardscene-update-", dir="/tmp"
        ) as name:
            path = copy_database(Path(name))
            connection = sqlite3.connect(
                path.as_uri() + "?mode=rw", uri=True, timeout=5
            )
            try:
                if (
                    connection.execute("PRAGMA application_id").fetchone()
                    != (application_id,)
                    or connection.execute("PRAGMA journal_mode").fetchone() != ("wal",)
                    or connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]
                    or connection.execute("PRAGMA foreign_key_check").fetchone()
                    is not None
                ):
                    raise InstallError("update_database_unrecognized")
                revisions = connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchall()
            finally:
                connection.close()
        if revisions == [(state.from_schema,)]:
            return "old"
        if revisions == [(state.to_schema,)]:
            return "target"
    except (OSError, sqlite3.Error) as error:
        raise InstallError("update_database_unrecognized") from error
    raise InstallError("update_database_unrecognized")
