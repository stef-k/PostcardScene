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
    with directory(KEY.parent) as parent:
        require_directory(parent, web, private, 0o700)
        fd = os.open(
            "backup.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
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
            # Never unlink this inode: all invocations must contend on one lock.
            yield fd
        finally:
            os.close(fd)
