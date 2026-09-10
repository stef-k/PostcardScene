"""Flat v1 sensitive archive format and immutable verified recovery identity."""

import gzip
import hashlib
import io
import json
import re
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone

from postcardscene.persistence import APPLICATION_ID, SCHEMA_REVISION, Database

from .files import (
    CHUNK,
    DB_NAME,
    MANIFEST_NAME,
    MEMBER_LIMITS,
    BackupError,
    private_file,
)

CATALOG = "regenerable_reconcile_required"
VERSION = (
    r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?"
)
NAME = re.compile(
    rf"postcardscene-backup-([0-9]{{8}}T[0-9]{{12}}Z)-v({VERSION})\.tar\.gz", re.ASCII
)
STAMP = "%Y%m%dT%H%M%S%fZ"
HASH = re.compile(r"[0-9a-f]{64}", re.ASCII)


@dataclass(frozen=True)
class VerifiedBackup:
    archive_filename: str
    archive_sha256: str
    created_at_utc: str
    application_version: str
    sqlite_application_id: int
    schema_revision: str
    catalog_classification: str


def filename(created, version):
    name = f"postcardscene-backup-{created.strftime(STAMP)}-v{version}.tar.gz"
    if not NAME.fullmatch(name):
        raise BackupError("unsupported_version")
    return name


def digest(stream, progress):
    value = hashlib.sha256()
    while block := stream.read(CHUNK):
        value.update(block)
        progress()
    return value.hexdigest()


def check_database(path):
    database = Database(path)
    try:
        return database.check()
    finally:
        database.engine.dispose()


class ProgressReader:
    """Keep compression progress visible while tarfile copies a large DB."""

    def __init__(self, source, progress):
        self.source = source
        self.progress = progress

    def read(self, size):
        value = self.source.read(size)
        self.progress()
        return value


def build(scratch, created, version, progress):
    members = {}
    for name in MEMBER_LIMITS:
        path = scratch / name
        with path.open("rb") as source:
            members[name] = {
                "size": path.stat().st_size,
                "sha256": digest(source, progress),
            }
    manifest = {
        "archive_schema": 1,
        "archive_kind": "postcardscene-backup",
        "created_at_utc": created.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
        "application_version": version,
        "sqlite_application_id": APPLICATION_ID,
        "schema_revision": SCHEMA_REVISION,
        "catalog_classification": CATALOG,
        "members": members,
    }
    data = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    path = scratch / filename(created, version)
    with (
        private_file(path) as output,
        tarfile.open(
            fileobj=output, mode="w:gz", format=tarfile.USTAR_FORMAT
        ) as archive,
    ):
        for name in (*MEMBER_LIMITS, MANIFEST_NAME):
            info = tarfile.TarInfo(name)
            info.mode = 0o600
            info.size = len(data) if name == MANIFEST_NAME else members[name]["size"]
            if name == MANIFEST_NAME:
                archive.addfile(info, io.BytesIO(data))
            else:
                with (scratch / name).open("rb") as source:
                    archive.addfile(info, ProgressReader(source, progress))
            progress()
    return path


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BackupError("invalid_manifest")
        result[key] = value
    return result


def validate_manifest(data, name):
    manifest = json.loads(data, object_pairs_hook=unique_object)
    expected = {
        "archive_schema",
        "archive_kind",
        "created_at_utc",
        "application_version",
        "sqlite_application_id",
        "schema_revision",
        "catalog_classification",
        "members",
    }
    if not isinstance(manifest, dict) or manifest.keys() != expected:
        raise BackupError("invalid_manifest")
    match = NAME.fullmatch(name)
    if match is None:
        raise BackupError("invalid_name")
    created = datetime.strptime(match[1], STAMP).replace(tzinfo=timezone.utc)
    if (
        type(manifest["archive_schema"]) is not int
        or manifest["archive_schema"] != 1
        or manifest["archive_kind"] != "postcardscene-backup"
        or manifest["created_at_utc"]
        != created.isoformat(timespec="microseconds").replace("+00:00", "Z")
        or manifest["application_version"] != match[2]
        or type(manifest["sqlite_application_id"]) is not int
        or manifest["sqlite_application_id"] != APPLICATION_ID
        or manifest["schema_revision"] != SCHEMA_REVISION
        or manifest["catalog_classification"] != CATALOG
    ):
        raise BackupError("incompatible_manifest")
    members = manifest["members"]
    if not isinstance(members, dict) or members.keys() != MEMBER_LIMITS.keys():
        raise BackupError("invalid_manifest")
    for member, limit in MEMBER_LIMITS.items():
        value = members[member]
        if (
            not isinstance(value, dict)
            or value.keys() != {"size", "sha256"}
            or type(value["size"]) is not int
            or not 0 < value["size"] <= limit
            or not isinstance(value["sha256"], str)
            or not HASH.fullmatch(value["sha256"])
        ):
            raise BackupError("invalid_manifest")
    if members["session.key"]["size"] != 32:
        raise BackupError("invalid_manifest")
    return manifest


def archive_members(source, progress):
    """Read only basic ustar headers: extension headers never get parsed."""
    limits = {**MEMBER_LIMITS, MANIFEST_NAME: 8192}
    seen = set()
    while True:
        header = source.read(512)
        if header == bytes(512):
            # Require the second EOF block and bounded zero record padding. Read
            # through gzip EOF to check its CRC and reject concatenated content.
            tail = source.read(10241)
            if len(tail) < 512 or len(tail) > 10240 or any(tail):
                raise BackupError("unsafe_archive")
            break
        if len(header) != 512 or len(seen) >= 4:
            raise BackupError("unsafe_archive")
        member = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
        if (
            member.name not in limits
            or member.name in seen
            or member.type != tarfile.REGTYPE
            or member.linkname
            or member.mode != 0o600
            or member.uid != 0
            or member.gid != 0
            or member.uname
            or member.gname
            or not 0 < member.size <= limits[member.name]
        ):
            raise BackupError("unsafe_archive")
        seen.add(member.name)
        yield member
        padding = source.read(-member.size % 512)
        if len(padding) != -member.size % 512 or any(padding):
            raise BackupError("unsafe_archive")
        progress()
    if seen != limits.keys():
        raise BackupError("unsafe_archive")


def member_bytes(source, size, progress):
    while size:
        block = source.read(min(CHUNK, size))
        if not block:
            raise BackupError("unsafe_archive")
        size -= len(block)
        progress()
        yield block


def unpack(archive_path, scratch, progress):
    """Validate all structure before extracting fixed files to private scratch."""
    with gzip.open(archive_path, "rb") as source:
        for member in archive_members(source, progress):
            for _ in member_bytes(source, member.size, progress):
                pass
    with gzip.open(archive_path, "rb") as source:
        for member in archive_members(source, progress):
            with private_file(scratch / member.name) as target:
                for block in member_bytes(source, member.size, progress):
                    target.write(block)


def verify_local(archive_path, archive_hash, scratch, progress):
    unpack(archive_path, scratch, progress)
    manifest = validate_manifest(
        (scratch / MANIFEST_NAME).read_bytes(), archive_path.name
    )
    for name, value in manifest["members"].items():
        path = scratch / name
        with path.open("rb") as source:
            if (
                path.stat().st_size != value["size"]
                or digest(source, progress) != value["sha256"]
            ):
                raise BackupError("member_mismatch")
    check_database(scratch / DB_NAME)
    return VerifiedBackup(
        archive_path.name,
        archive_hash,
        manifest["created_at_utc"],
        manifest["application_version"],
        manifest["sqlite_application_id"],
        manifest["schema_revision"],
        manifest["catalog_classification"],
    )
