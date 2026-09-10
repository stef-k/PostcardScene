"""Bounded exact-pair retention, only inside the destination worker after creation."""

import gzip
import hashlib
import os

from .archive import NAME, archive_members, member_bytes, validate_manifest
from .destination import ENTRY_LIMIT, checksum
from .files import ARCHIVE_LIMIT, MANIFEST_NAME, BackupError, directory, regular


def identity(parent, name):
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def pair_identity(parent, name):
    return identity(parent, name), identity(parent, name + ".sha256")


def owned_pair(parent, name, progress):
    """Check hashes, bounded flat structure and manifest, without opening SQLite."""
    before = pair_identity(parent, name)
    expected = checksum(parent, name)
    with regular(parent, name, ARCHIVE_LIMIT) as stream:
        # Limit even a concurrently growing file, not merely its initial stat size.
        value = hashlib.sha256()
        for block in member_bytes(stream, os.fstat(stream.fileno()).st_size, progress):
            value.update(block)
        if stream.read(1) or value.hexdigest() != expected:
            raise BackupError("archive_mismatch")
        stream.seek(0)
        members = {}
        manifest_data = b""
        with gzip.GzipFile(fileobj=stream) as source:
            for member in archive_members(source, progress):
                value = hashlib.sha256()
                for block in member_bytes(source, member.size, progress):
                    value.update(block)
                    if member.name == MANIFEST_NAME:
                        manifest_data += block
                members[member.name] = {
                    "size": member.size,
                    "sha256": value.hexdigest(),
                }
        manifest = validate_manifest(manifest_data, name)
        del members[MANIFEST_NAME]
        if members != manifest["members"]:
            raise BackupError("member_mismatch")
    if pair_identity(parent, name) != before:
        raise BackupError("retention_authority_changed")
    return before, expected


def retain(destination, count, new_name, new_hash, progress):
    if type(count) is not int or not 1 <= count <= 30 or not NAME.fullmatch(new_name):
        raise BackupError("retention_invalid")
    with directory(destination) as parent:
        # The supplied identity came from create's full verification. Recheck the
        # pair on this destination before granting any deletion authority.
        new_identity, actual = owned_pair(parent, new_name, progress)
        if actual != new_hash:
            raise BackupError("retention_authority_changed")
        names = set()
        with os.scandir(parent) as entries:
            for index, entry in enumerate(entries):
                if index >= ENTRY_LIMIT:
                    raise BackupError("entry_limit")
                if NAME.fullmatch(entry.name):
                    names.add(entry.name)
                progress()
        candidates = []
        for name in sorted(names - {new_name}):
            try:
                pair, _ = owned_pair(parent, name, progress)
            except Exception:
                # Foreign, incomplete and malformed pairs confer no authority.
                continue
            candidates.append((name, pair))
        # Reserve one slot for the new verified recovery point even after a
        # backwards wall-clock correction makes its filename sort before others.
        for name, pair in candidates[: max(0, len(candidates) - count + 1)]:
            if (
                pair_identity(parent, new_name) != new_identity
                or pair_identity(parent, name) != pair
            ):
                raise BackupError("retention_authority_changed")
            os.unlink(name, dir_fd=parent)
            # Revalidate the remaining exact sidecar immediately before unlink.
            if identity(parent, name + ".sha256") != pair[1]:
                raise BackupError("retention_authority_changed")
            os.unlink(name + ".sha256", dir_fd=parent)
            os.fsync(parent)
            progress()
    return {"result": "ready"}
