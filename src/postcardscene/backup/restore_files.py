"""Bounded raw rollback capture and per-filesystem atomic durable replacement."""

import hashlib
import json
import os
import stat
import time
import uuid
from dataclasses import asdict, dataclass

from . import capture
from .files import (
    CHUNK,
    DB_NAME,
    MEMBER_LIMITS,
    BackupError,
    directory,
    private_file,
    regular,
)


@dataclass(frozen=True, repr=False)
class FileState:
    size: int
    sha256: str
    uid: int
    gid: int
    mode: int
    atime_ns: int
    mtime_ns: int
    attributes: tuple


def targets(identities):
    runtime, web, shared, private = identities
    return (
        (capture.CONFIG, (0, shared, 0o640), (0, shared, 0o750), 65536),
        (
            capture.DATABASE,
            (runtime, shared, 0o660),
            (runtime, shared, 0o2770),
            MEMBER_LIMITS[DB_NAME],
        ),
        (capture.KEY, (web, shared, 0o600), (web, private, 0o700), 65536),
    )


def transfer(source, target=None, limit=MEMBER_LIMITS[DB_NAME]):
    digest = hashlib.sha256()
    total = 0
    deadline = time.monotonic() + 120
    while block := source.read(min(CHUNK, limit - total + 1)):
        total += len(block)
        if total > limit or time.monotonic() >= deadline:
            raise BackupError("restore_copy_failed")
        digest.update(block)
        if target is not None:
            target.write(block)
    return total, digest.hexdigest()


def describe(source, target, limit):
    fd = source.fileno()
    before = os.fstat(fd)
    attrs = tuple((name, os.getxattr(fd, name)) for name in sorted(os.listxattr(fd)))
    size, digest = transfer(source, target, limit)
    after = os.fstat(fd)
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise BackupError("restore_current_changed")
    return FileState(
        size,
        digest,
        before.st_uid,
        before.st_gid,
        stat.S_IMODE(before.st_mode),
        before.st_atime_ns,
        before.st_mtime_ns,
        attrs,
    )


def current(path, metadata, parent_metadata, limit, destination=None):
    with directory(path.parent) as parent:
        capture.require_directory(parent, *parent_metadata)
        # O_NOATIME preserves the rollback timestamp even during proof reads.
        fd = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOATIME,
            dir_fd=parent,
        )
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > limit
                or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != metadata
            ):
                raise BackupError("restore_host_invalid")
            return describe(source, destination, limit)


def file_set(identities, *, generated=False):
    entries = list(targets(identities))
    runtime, web, shared, _ = identities
    for suffix in ("-wal", "-shm"):
        path = capture.DATABASE.with_name(capture.DATABASE.name + suffix)
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if info.st_uid not in ((runtime, web, 0) if generated else (runtime, web)):
            raise BackupError("restore_host_invalid")
        entries.append(
            (
                path,
                (info.st_uid, shared, 0o660),
                (runtime, shared, 0o2770),
                MEMBER_LIMITS[DB_NAME],
            )
        )
    return entries


def capture_current(root, identities):
    result = []
    for entry in file_set(identities):
        path, metadata, parent_metadata, limit = entry
        with private_file(root / path.name) as output:
            identity = current(*entry, destination=output)
            output.flush()
            os.fsync(output.fileno())
        result.append((entry, identity))
    manifest = []
    for entry, identity in result:
        record = asdict(identity)
        record["attributes"] = [
            (name, value.hex()) for name, value in identity.attributes
        ]
        manifest.append({"name": entry[0].name, **record})
    with private_file(root / "rollback-metadata.json") as output:
        output.write(json.dumps(manifest, sort_keys=True).encode())
        output.flush()
        os.fsync(output.fileno())
    with directory(root) as parent:
        os.fsync(parent)
    return tuple(result)


def replace(source_parent, source_name, entry, expected, *, rollback=None):
    path, metadata, parent_metadata, limit = entry
    temporary = ".postcardscene-restore-" + uuid.uuid4().hex
    with directory(path.parent) as parent:
        capture.require_directory(parent, *parent_metadata)
        try:
            with regular(
                source_parent,
                source_name,
                limit,
                metadata=(0, 0, 0o600),
                allow_empty=True,
            ) as source:
                fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent,
                )
                with os.fdopen(fd, "wb") as target:
                    if transfer(source, target, limit) != expected:
                        raise BackupError("restore_candidate_invalid")
                    target.flush()
                    os.fchown(target.fileno(), *metadata[:2])
                    os.fchmod(target.fileno(), metadata[2])
                    if rollback is not None:
                        for name, value in rollback.attributes:
                            os.setxattr(target.fileno(), name, value)
                        os.utime(
                            target.fileno(), ns=(rollback.atime_ns, rollback.mtime_ns)
                        )
                    os.fsync(target.fileno())
            os.fsync(parent)
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def remove_sidecars(identities, captured=None, *, generated=False):
    for entry in file_set(identities, generated=generated)[3:]:
        observed = current(*entry)
        if captured is not None and (entry, observed) not in captured:
            raise BackupError("restore_current_changed")
        path = entry[0]
        with directory(path.parent) as parent:
            os.unlink(path.name, dir_fd=parent)
            os.fsync(parent)


def rollback(root, identities, captured):
    remove_sidecars(identities, generated=True)
    with directory(root) as source:
        for entry, identity in captured:
            replace(
                source,
                entry[0].name,
                entry,
                (identity.size, identity.sha256),
                rollback=identity,
            )
    if {entry[0] for entry in file_set(identities)} != {
        entry[0] for entry, _ in captured
    }:
        raise BackupError("restore_rollback_unproven")
    for entry, identity in captured:
        if current(*entry) != identity:
            raise BackupError("restore_rollback_unproven")
