"""Fixed private file and archive bounds for the managed Linux backup boundary."""

import os
import stat
from contextlib import contextmanager
from pathlib import Path

DB_NAME = "postcardscene.sqlite3"
MANIFEST_NAME = "backup-manifest.json"
MEMBER_LIMITS = {DB_NAME: 4 * 1024**3, "config.py": 65536, "session.key": 32}
ARCHIVE_LIMIT = 5 * 1024**3
CHUNK = 1024 * 1024


class BackupError(RuntimeError):
    """Safe fixed failure category; never include underlying exception text."""


@contextmanager
def directory(path):
    """Pin each directory component without following symlinks, including parents."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise BackupError("unsafe_path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def regular(parent, name, limit, *, metadata=None, allow_empty=False):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or not (0 if allow_empty else 1) <= info.st_size <= limit
        ):
            raise BackupError("unsafe_file")
        if (
            metadata is not None
            and (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != metadata
        ):
            raise BackupError("managed_authority_invalid")
        yield stream


@contextmanager
def private_output(fd):
    """Fail before writing secrets when a filesystem cannot honor private modes."""
    with os.fdopen(fd, "wb") as stream:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise BackupError("private_output_unavailable")
        yield stream


def private_file(path):
    return private_output(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    )


def copy_bytes(source, target, limit, progress):
    """Bound memory and bytes, reporting progress only after completed I/O."""
    total = 0
    while block := source.read(min(CHUNK, limit - total + 1)):
        total += len(block)
        if total > limit:
            raise BackupError("size_limit")
        target.write(block)
        progress()
    return total
