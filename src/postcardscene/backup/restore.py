"""Root-only, non-destructive restore intake and local candidate revalidation."""

import os
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from importlib.metadata import version
from pathlib import Path

from sqlalchemy import select

from postcardscene.accounts import Administrator
from postcardscene.persistence import APPLICATION_ID, SCHEMA_REVISION, Database

from . import archive, capture, destination
from .files import (
    ARCHIVE_LIMIT,
    DB_NAME,
    MANIFEST_NAME,
    MEMBER_LIMITS,
    BackupError,
    copy_bytes,
    directory,
    private_file,
    regular,
)


@dataclass(frozen=True, repr=False)
class StagedMember:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class PreparedRestore:
    """Private in-process authority, never serialize paths or member identities."""

    verified: archive.VerifiedBackup
    staging_root: Path = field(repr=False)
    root_identity: tuple[int, int] = field(repr=False)
    members: tuple[StagedMember, ...] = field(repr=False)


def require_root():
    if os.geteuid() != 0:
        raise BackupError("restore_root_required")


@contextmanager
def staging_directory(path, expected=None):
    """Pin a direct local /tmp child; never traverse caller-controlled parents."""
    require_root()
    path = Path(path)
    if path.parent != Path("/tmp") or path.name in ("", ".", ".."):
        raise BackupError("restore_staging_invalid")
    with directory(Path("/tmp")) as temporary:
        parent = os.fstat(temporary)
        if parent.st_uid != 0 or stat.S_IMODE(parent.st_mode) != 0o1777:
            raise BackupError("restore_staging_invalid")
        fd = os.open(
            path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=temporary
        )
        try:
            info = os.fstat(fd)
            identity = (info.st_dev, info.st_ino)
            if (
                info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o700
                or info.st_dev != parent.st_dev
                or (expected is not None and identity != expected)
            ):
                raise BackupError("restore_staging_invalid")
            yield fd, identity
        finally:
            os.close(fd)


def member_identities(parent, name, progress):
    limits = {**MEMBER_LIMITS, MANIFEST_NAME: 8192, name: ARCHIVE_LIMIT}
    if set(os.listdir(parent)) != limits.keys():
        raise BackupError("restore_staging_invalid")
    members = []
    for member, limit in limits.items():
        with regular(parent, member, limit) as source:
            info = os.fstat(source.fileno())
            if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
                raise BackupError("restore_staging_invalid")
            members.append(
                StagedMember(member, info.st_size, archive.digest(source, progress))
            )
    return tuple(members)


def validate_semantics(scratch, verified):
    if verified.application_version != version("postcardscene"):
        raise BackupError("restore_version_incompatible")
    capture.validate_config((scratch / "config.py").read_bytes())
    if (scratch / "session.key").stat().st_size != 32:
        raise BackupError("restore_key_invalid")
    database = Database(scratch / DB_NAME)
    try:
        database.check()
        with database.transaction() as session:
            if session.scalar(select(Administrator.id).limit(1)) is None:
                raise BackupError("restore_administrator_missing")
    finally:
        database.engine.dispose()


def stage_restore(path, staging_root, expected, progress):
    """Worker operation: finish all destination I/O before returning identity."""
    path = Path(path)
    if not archive.NAME.fullmatch(path.name):
        raise BackupError("invalid_name")
    with staging_directory(staging_root, expected) as (parent, _):
        if os.listdir(parent):
            raise BackupError("restore_staging_invalid")
        # Existing archive helpers use paths. Resolve through the pinned descriptor,
        # so a renamed /tmp entry cannot redirect extraction or SQLite authority.
        scratch = Path(f"/proc/self/fd/{parent}")
        local = scratch / path.name
        with directory(path.parent) as source_parent:
            expected_hash = destination.checksum(source_parent, path.name)
            with (
                regular(source_parent, path.name, ARCHIVE_LIMIT) as source,
                private_file(local) as target,
            ):
                copy_bytes(source, target, ARCHIVE_LIMIT, progress)
        with local.open("rb") as source:
            actual = archive.digest(source, progress)
        if actual != expected_hash:
            raise BackupError("archive_mismatch")
        verified = archive.verify_local(local, actual, scratch, progress)
        before = member_identities(parent, path.name, progress)
        validate_semantics(scratch, verified)
        if member_identities(parent, path.name, progress) != before:
            raise BackupError("restore_staging_changed")
        return {"verified": asdict(verified), "members": [asdict(m) for m in before]}


def cleanup_staging(path, identity):
    """Unlink only direct scratch entries; never follow links or recurse."""
    with staging_directory(path, identity) as (parent, _):
        for name in os.listdir(parent):
            os.unlink(name, dir_fd=parent)
        os.rmdir(path)


def prepare_restore(path, staging_root, **bounds):
    """Consume one archive pair into the caller's fresh root-private /tmp root.

    On success the caller owns the candidate and its sensitive scratch lifetime.
    On failure only accepted local scratch is cleaned, never managed host state.
    """
    from . import _run

    require_root()
    identity = None
    try:
        root = Path(staging_root)
        with staging_directory(root) as (parent, checked):
            if os.listdir(parent):
                raise BackupError("restore_staging_invalid")
            identity = checked
        result = _run(
            "restore-stage",
            path,
            arguments=(str(root), *(str(value) for value in identity)),
            **bounds,
        )
        candidate = PreparedRestore(
            archive.VerifiedBackup(**result["verified"]),
            root,
            identity,
            tuple(StagedMember(**member) for member in result["members"]),
        )
        revalidate_restore(candidate)
        return candidate
    except (Exception, KeyboardInterrupt) as error:
        category = "restore_preparation_failed"
        if isinstance(error, BackupError) and str(error) in {
            "operation_timeout_or_cancelled",
            "worker_cleanup_uncertain",
        }:
            category = str(error)
        # A kill-pending worker may still own local files: do not race its cleanup.
        if identity is not None and category != "worker_cleanup_uncertain":
            try:
                cleanup_staging(root, identity)
            except Exception:
                category = "restore_cleanup_uncertain"
        raise BackupError(category) from None


def revalidate_restore(candidate):
    """Check only local identity immediately before #170 stages target files."""
    require_root()
    try:
        if (
            candidate.verified.application_version != version("postcardscene")
            or candidate.verified.sqlite_application_id != APPLICATION_ID
            or candidate.verified.schema_revision != SCHEMA_REVISION
        ):
            raise BackupError("restore_version_incompatible")
        with staging_directory(candidate.staging_root, candidate.root_identity) as (
            parent,
            _,
        ):
            members = member_identities(
                parent, candidate.verified.archive_filename, lambda: None
            )
        if members != candidate.members:
            raise BackupError("restore_staging_changed")
    except Exception:
        raise BackupError("restore_candidate_invalid") from None
