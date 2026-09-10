"""Deterministic extracted release-input validation; no host mutation or imports."""

import ast
import base64
import configparser
import csv
import email.parser
import hashlib
import io
import json
import os
import re
import stat
import zipfile


class InstallError(Exception):
    """Only fixed safe reasons cross the installer output boundary."""


def read_input(path, limit=32 * 1024 * 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise InstallError("invalid_bundle_input")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise InstallError("invalid_bundle_input")
        return data


def validate_wheel(data, filename, assets):
    match = re.fullmatch(r"postcardscene-([0-9][a-z0-9.]*)-py3-none-any.whl", filename)
    if not match:
        raise InstallError("invalid_wheel_identity")
    version = match[1]
    info = f"postcardscene-{version}.dist-info"
    with zipfile.ZipFile(io.BytesIO(data)) as wheel:
        names = wheel.namelist()
        if (
            len(names) != len(set(names))
            or sum(i.file_size for i in wheel.infolist()) > 64 * 1024 * 1024
        ):
            raise InstallError("invalid_wheel_members")
        for entry in wheel.infolist():
            name = entry.filename
            if (
                not name.startswith(("postcardscene/", info + "/"))
                or any(part in ("", ".", "..") for part in name.rstrip("/").split("/"))
                or "\\" in name
                or stat.S_ISLNK(entry.external_attr >> 16)
                or entry.flag_bits & 1
                or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            ):
                raise InstallError("invalid_wheel_members")
        metadata = email.parser.BytesParser().parsebytes(wheel.read(f"{info}/METADATA"))
        tags = email.parser.BytesParser().parsebytes(wheel.read(f"{info}/WHEEL"))
        if (
            metadata["Name"] != "postcardscene"
            or metadata["Version"] != version
            or set(metadata.get("Requires-Python", "").replace(" ", "").split(","))
            != {">=3.11", "<3.15"}
            or tags["Root-Is-Purelib"] != "true"
            or tags.get_all("Tag") != ["py3-none-any"]
        ):
            raise InstallError("invalid_wheel_metadata")
        entries = configparser.ConfigParser()
        entries.read_string(wheel.read(f"{info}/entry_points.txt").decode())
        if dict(entries["console_scripts"]) != {
            "postcardscene-doctor": "postcardscene.doctor.cli:main",
            "postcardscene-web": "postcardscene.web.server:main",
            "postcardscene-runtime": "postcardscene.runtime.cli:main",
            "postcardscene-input-emitter": "postcardscene.graphics.local_input:main",
        }:
            raise InstallError("invalid_wheel_entry_points")
        validate_record(wheel, info)
        for name in (
            *assets,
            "graphics/labwc/rc.xml",
            "graphics/labwc/autostart",
            "migrations/env.py",
        ):
            wheel.read("postcardscene/" + name)
    return version


def validate_record(wheel, info):
    record = f"{info}/RECORD"
    rows = list(csv.reader(io.StringIO(wheel.read(record).decode())))
    files = {entry.filename for entry in wheel.infolist() if not entry.is_dir()}
    if (
        any(len(row) != 3 for row in rows)
        or len(rows) != len(files)
        or {r[0] for r in rows} != files
    ):
        raise InstallError("invalid_wheel_record")
    for name, digest, size in rows:
        if name == record:
            if digest or size:
                raise InstallError("invalid_wheel_record")
            continue
        content = wheel.read(name)
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(content).digest())
            .decode()
            .rstrip("=")
        )
        if digest != "sha256=" + expected or size != str(len(content)):
            raise InstallError("invalid_wheel_record")


def validate_inputs(bundle, members, assets):
    wheel_name = next(name for name in members if name.endswith(".whl"))
    data = members[wheel_name]
    version = validate_wheel(data, wheel_name, assets)
    return version, wheel_name, data, members["runtime-requirements.txt"]


SUPPORT_NAMES = (
    "install.py",
    "install_inputs.py",
    "install_host.py",
    "install_preflight.py",
    "runtime-requirements.txt",
)
MANIFEST_NAME = "release-manifest.json"
MANAGED_TARGETS = {
    "architecture": "arm64",
    "host": "native-linux-systemd",
    "distros": ["ubuntu-24.04", "ubuntu-26.04", "debian-13-trixie"],
    "hardware_evidence": "not-established-by-artifact-smoke",
}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InstallError("invalid_release_manifest")
        result[key] = value
    return result


def wheel_identity(data, version):
    """Read packaged metadata/constants without executing application code."""
    with zipfile.ZipFile(io.BytesIO(data)) as wheel:
        metadata = email.parser.BytesParser().parsebytes(
            wheel.read(f"postcardscene-{version}.dist-info/METADATA")
        )
        constants = {}
        tree = ast.parse(wheel.read("postcardscene/persistence.py"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id in (
                    "APPLICATION_ID",
                    "SCHEMA_REVISION",
                ):
                    constants[target.id] = ast.literal_eval(node.value)
        schema = {
            "application_id": constants["APPLICATION_ID"],
            "alembic_head": constants["SCHEMA_REVISION"],
        }
        revisions = {}
        for name in wheel.namelist():
            if name.startswith("postcardscene/migrations/versions/") and name.endswith(
                ".py"
            ):
                values = {}
                for node in ast.parse(wheel.read(name)).body:
                    if isinstance(node, ast.Assign) and len(node.targets) == 1:
                        target = node.targets[0]
                        if isinstance(target, ast.Name) and target.id in (
                            "revision",
                            "down_revision",
                        ):
                            values[target.id] = ast.literal_eval(node.value)
                if "revision" in values:
                    revisions[values["revision"]] = values["down_revision"]
        if set(revisions) - set(revisions.values()) != {schema["alembic_head"]}:
            raise InstallError("invalid_release_schema")
        if metadata["Version"] != version:
            raise InstallError("invalid_release_identity")
        return metadata["Requires-Python"], schema


def validate_manifest(bundle, pinned):
    """Gate the fixed extracted set; return the same bytes used after validation."""
    manifest = json.loads(
        read_input(bundle / MANIFEST_NAME, 64 * 1024), object_pairs_hook=unique_object
    )
    fields = {
        "manifest_schema",
        "version",
        "source_commit",
        "tag",
        "requires_python",
        "managed_targets",
        "schema",
        "members",
    }
    if not isinstance(manifest, dict) or set(manifest) != fields:
        raise InstallError("invalid_release_manifest")
    version = manifest["version"]
    if (
        type(manifest["manifest_schema"]) is not int
        or manifest["manifest_schema"] != 1
        or not isinstance(version, str)
        or not re.fullmatch(r"[0-9][a-z0-9.]*", version)
        or manifest["tag"] != "v" + version
        or not isinstance(manifest["source_commit"], str)
        or not re.fullmatch(r"[0-9a-f]{40}", manifest["source_commit"])
        or manifest["managed_targets"] != MANAGED_TARGETS
    ):
        raise InstallError("invalid_release_identity")
    wheel_name = f"postcardscene-{version}-py3-none-any.whl"
    names = {*SUPPORT_NAMES, wheel_name}
    if (
        not isinstance(manifest["members"], dict)
        or set(manifest["members"]) != names
        or {path.name for path in bundle.iterdir()} != names | {MANIFEST_NAME}
    ):
        raise InstallError("invalid_release_members")
    members = {}
    for name in sorted(names):
        data = read_input(bundle / name)
        entry = manifest["members"][name]
        if (
            not isinstance(entry, dict)
            or set(entry) != {"size", "sha256"}
            or type(entry["size"]) is not int
            or entry["size"] != len(data)
            or entry["sha256"] != hashlib.sha256(data).hexdigest()
            or (name in pinned and data != pinned[name])
        ):
            raise InstallError("release_member_mismatch")
        members[name] = data
    python, schema = wheel_identity(members[wheel_name], version)
    if manifest["requires_python"] != python or manifest["schema"] != schema:
        raise InstallError("invalid_release_identity")
    return members
