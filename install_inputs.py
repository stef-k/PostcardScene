"""Deterministic extracted release-input validation; no host mutation or imports."""

import base64
import configparser
import csv
import email.parser
import hashlib
import io
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


def validate_inputs(bundle, requirements, assets):
    wheels = list(bundle.glob("*.whl"))
    if len(wheels) != 1:
        raise InstallError("expected_one_application_wheel")
    data = read_input(wheels[0])
    version = validate_wheel(data, wheels[0].name, assets)
    return version, wheels[0].name, data, requirements
