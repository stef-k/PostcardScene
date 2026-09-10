"""Destination I/O, called only inside the disposable bounded worker."""

import ctypes
import errno
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from .archive import HASH, NAME, digest, verify_local
from .files import (
    ARCHIVE_LIMIT,
    BackupError,
    copy_bytes,
    directory,
    private_file,
    regular,
)

ENTRY_LIMIT = 1000


@dataclass(frozen=True)
class BackupEntry:
    archive_filename: str
    state: str


def rename_exclusive(parent, old, new):
    """Linux atomic rename with RENAME_NOREPLACE; unsupported filesystems fail."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if rename(parent, os.fsencode(old), parent, os.fsencode(new), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, errno.errorcode.get(code, "rename_failed"))


def checksum(parent, name):
    with regular(parent, name + ".sha256", 512) as source:
        data = source.read(513)
    expected_length = 64 + 2 + len(name) + 1
    if len(data) != expected_length:
        raise BackupError("invalid_checksum")
    text = data.decode("ascii")
    if not HASH.fullmatch(text[:64]) or text[64:] != f"  {name}\n":
        raise BackupError("invalid_checksum")
    return text[:64]


def verify_at(parent, name, progress):
    if not NAME.fullmatch(name):
        raise BackupError("invalid_name")
    expected = checksum(parent, name)
    # Force local scratch; TMPDIR must never redirect staging onto the destination.
    with tempfile.TemporaryDirectory(
        prefix="postcardscene-backup-", dir="/tmp"
    ) as temp:
        scratch = Path(temp)
        local = scratch / name
        with (
            regular(parent, name, ARCHIVE_LIMIT) as source,
            private_file(local) as target,
        ):
            copy_bytes(source, target, ARCHIVE_LIMIT, progress)
        with local.open("rb") as source:
            actual = digest(source, progress)
        if actual != expected:
            raise BackupError("archive_mismatch")
        return verify_local(local, actual, scratch, progress)


def verify(path, progress):
    path = Path(path)
    with directory(path.parent) as parent:
        return verify_at(parent, path.name, progress)


def publish(archive, destination, progress):
    with archive.open("rb") as source:
        value = digest(source, progress)
    name = archive.name
    with directory(destination) as parent:
        # Check both first, and still require no-replace for each final rename.
        for final in (name, name + ".sha256"):
            try:
                os.stat(final, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise BackupError("name_collision")
        temporary = ".postcardscene-backup-" + uuid.uuid4().hex
        for suffix in ("", ".sha256"):
            fd = os.open(
                temporary + suffix,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            with os.fdopen(fd, "wb") as target:
                if suffix:
                    target.write(f"{value}  {name}\n".encode("ascii"))
                else:
                    with archive.open("rb") as source:
                        copy_bytes(source, target, ARCHIVE_LIMIT, progress)
                target.flush()
                os.fsync(target.fileno())
            progress()
        rename_exclusive(parent, temporary, name)
        os.fsync(parent)
        progress()
        rename_exclusive(parent, temporary + ".sha256", name + ".sha256")
        os.fsync(parent)
        progress()
        return verify_at(parent, name, progress)


def list_backups(destination, progress):
    with directory(destination) as parent:
        names = set()
        with os.scandir(parent) as entries:
            for index, entry in enumerate(entries):
                if index >= ENTRY_LIMIT:
                    raise BackupError("entry_limit")
                base = entry.name.removesuffix(".sha256")
                if NAME.fullmatch(base):
                    names.add(entry.name)
                progress()
        result = []
        for name in sorted({n.removesuffix(".sha256") for n in names}):
            state = "incomplete"
            if name in names and name + ".sha256" in names:
                try:
                    verify_at(parent, name, progress)
                    state = "verified"
                except Exception:
                    state = "invalid"
            result.append(BackupEntry(name, state))
        return tuple(result)
