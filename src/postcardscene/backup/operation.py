"""One local, nonblocking lock for manual and scheduled backup mutation."""

import fcntl
import os
import stat
from contextlib import contextmanager

from .capture import KEY, identities, require_directory
from .files import BackupError, directory


@contextmanager
def operation_lock():
    _, web, shared, private = identities()
    if os.geteuid() != web:
        raise BackupError("web_identity_required")
    with _checked_lock(web, shared, private, create=True) as fd:
        yield fd


@contextmanager
def restore_lock():
    """Root bootstraps or contends on the canonical web-owned operation inode."""
    if os.geteuid() != 0:
        raise BackupError("restore_root_required")
    _, web, shared, private = identities()
    with _checked_lock(web, shared, private, create=False) as fd:
        yield fd


@contextmanager
def _checked_lock(web, shared, private, *, create):
    with directory(KEY.parent) as parent:
        require_directory(parent, web, private, 0o700)
        if not create:
            _bootstrap_restore_lock(parent, web, shared)
        fd = os.open(
            "backup.lock",
            os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if create else 0),
            0o600,
            dir_fd=parent,
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
                != (web, shared, 0o600)
            ):
                raise BackupError("operation_lock_invalid")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BackupError("operation_busy") from None
            current = os.stat("backup.lock", dir_fd=parent, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise BackupError("operation_lock_invalid")
            # Never unlink this inode: all invocations must contend on one lock.
            yield fd
        finally:
            os.close(fd)


def _bootstrap_restore_lock(parent, web, shared):
    try:
        fd = os.open(
            "backup.lock",
            os.O_RDWR | os.O_NOFOLLOW | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent,
        )
    except FileExistsError:
        return  # Validate the winner; never replace or repair an existing inode.
    try:
        os.fchown(fd, web, shared)
        os.fchmod(fd, 0o600)
        os.fsync(fd)
        os.fsync(parent)
    finally:
        os.close(fd)
