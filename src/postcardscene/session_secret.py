"""Explicit protected signing-key file lifecycle for a Linux installation."""

import os
import secrets
import stat
from pathlib import Path

DEFAULT_SECRET_PATH = Path("/var/lib/postcardscene-web/session.key")


def secret_path(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("SESSION_SECRET_PATH must be absolute.")
    parent = path.parent.stat()
    if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077:
        raise ValueError(
            "Signing-key directory must be owned by this user and private (0700)."
        )
    return path


def read_secret(path):
    path = secret_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise ValueError(
                "Signing key must be a private owner-only regular file (0600)."
            )
        value = source.read(33)
    if len(value) != 32:
        raise ValueError(
            "Signing key must contain exactly 32 bytes; do not overwrite it."
        )
    return value


def initialize_secret(path):
    """Never replace existing authority; repeat initialization validates it."""
    path = secret_path(path)
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
    except FileExistsError:
        read_secret(path)
        return
    with os.fdopen(descriptor, "wb") as destination:
        destination.write(secrets.token_bytes(32))
        destination.flush()
        os.fsync(destination.fileno())
