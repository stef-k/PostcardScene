"""Build and safely inspect the fixed native archive from reviewed source inputs."""

import gzip
import hashlib
import io
import json
import os
import runpy
import tarfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
INPUTS = SimpleNamespace(**runpy.run_path(str(ROOT / "install_inputs.py")))


def write_manifest(bundle, version, source_commit):
    wheel_name = f"postcardscene-{version}-py3-none-any.whl"
    python, schema = INPUTS.wheel_identity((bundle / wheel_name).read_bytes(), version)
    members = {}
    for name in sorted((*INPUTS.SUPPORT_NAMES, wheel_name)):
        data = INPUTS.read_input(bundle / name)
        members[name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    manifest = {
        "manifest_schema": 1,
        "version": version,
        "source_commit": source_commit,
        "tag": "v" + version,
        "requires_python": python,
        "managed_targets": INPUTS.MANAGED_TARGETS,
        "schema": schema,
        "members": members,
    }
    (bundle / INPUTS.MANIFEST_NAME).write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    )
    return manifest


def write_archive(bundle, destination, timestamp):
    # USTAR avoids extended/path metadata; gzip omits the local filename.
    with destination.open("xb") as output:
        with gzip.GzipFile(
            filename="", mode="wb", fileobj=output, mtime=timestamp
        ) as compressed:
            with tarfile.open(
                fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT
            ) as archive:
                for path in sorted(bundle.iterdir()):
                    data = INPUTS.read_input(path)
                    entry = tarfile.TarInfo(path.name)
                    entry.size = len(data)
                    entry.mode = 0o644
                    entry.mtime = timestamp
                    archive.addfile(entry, io.BytesIO(data))


def extract_archive(path, destination, version):
    """Validate every header and bounded body before creating any extracted file."""
    expected = {
        *INPUTS.SUPPORT_NAMES,
        INPUTS.MANIFEST_NAME,
        f"postcardscene-{version}-py3-none-any.whl",
    }
    members = {}
    with tarfile.open(path, "r:gz") as archive:
        for entry in archive:
            if (
                entry.name not in expected
                or entry.name in members
                or entry.type != tarfile.REGTYPE
                or entry.mode != 0o644
                or entry.uid != 0
                or entry.gid != 0
                or entry.uname
                or entry.gname
                or entry.linkname
                or entry.pax_headers
                or entry.devmajor
                or entry.devminor
                or not 0 <= entry.mtime <= 0xFFFFFFFF
                or not 0 <= entry.size <= 32 * 1024 * 1024
            ):
                raise ValueError("Unsafe or unexpected native archive member")
            members[entry.name] = archive.extractfile(entry).read()
    if set(members) != expected:
        raise ValueError("Incomplete native archive")
    destination.mkdir(mode=0o700)
    for name, data in members.items():
        with (destination / name).open("xb") as output:
            output.write(data)
        os.chmod(destination / name, 0o644)


def write_checksums(directory, names):
    text = "".join(
        f"{hashlib.sha256((directory / name).read_bytes()).hexdigest()}  {name}\n"
        for name in sorted(names)
    )
    (directory / "SHA256SUMS").write_text(text)


def verify_checksums(directory, names):
    expected = "".join(
        f"{hashlib.sha256(INPUTS.read_input(directory / name, 64 * 1024 * 1024)).hexdigest()}  {name}\n"
        for name in sorted(names)
    )
    if INPUTS.read_input(directory / "SHA256SUMS", 4096).decode() != expected:
        raise ValueError("Final artifact checksum mismatch")
