"""Build and smoke immutable native release bytes; publication is workflow-owned."""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from check_release_inputs import check_wheel, run
from release_bundle import (
    INPUTS,
    ROOT,
    extract_archive,
    verify_checksums,
    write_archive,
    write_checksums,
    write_manifest,
)


def github_prerelease(version):
    """Classify the validated, normalized package version's pre/dev segments."""
    return "true" if re.search(r"(?:a|b|rc|\.dev)[0-9]+", version) else "false"


def source_identity(project, require_tag):
    sha = run("git", "rev-parse", "HEAD", capture_output=True, text=True).stdout.strip()
    timestamp = int(
        run("git", "show", "-s", "--format=%ct", "HEAD", capture_output=True).stdout
    )
    if run("git", "status", "--porcelain", capture_output=True).stdout:
        raise ValueError("Release builds require a clean checkout")
    if require_tag:
        tag = "v" + project["version"]
        if os.environ.get("GITHUB_REF") != "refs/tags/" + tag:
            raise ValueError(
                "Release workflow must run on the exact package version tag"
            )
        tagged = run(
            "git",
            "rev-parse",
            f"refs/tags/{tag}^{{commit}}",
            capture_output=True,
            text=True,
        ).stdout.strip()
        if tagged != sha or os.environ.get("GITHUB_SHA") != sha:
            raise ValueError("Release tag/source mismatch")
    return sha, timestamp


def prepare_bundle(directory, wheel, version, sha):
    directory.mkdir()
    shutil.copyfile(wheel, directory / wheel.name)
    for name in INPUTS.SUPPORT_NAMES:
        shutil.copyfile(ROOT / name, directory / name)
    return write_manifest(directory, version, sha)


def smoke_archive(output, project, scratch, sha):
    version = project["version"]
    archive_name = f"postcardscene-{version}-linux-native.tar.gz"
    wheel_name = f"postcardscene-{version}-py3-none-any.whl"
    verify_checksums(output, (archive_name, wheel_name))
    extracted = scratch / "extracted"
    extract_archive(output / archive_name, extracted, version)
    # Run outside the source tree; no extracted code runs before external checksum.
    installer_smoke = scratch / "smoke_release_installer.py"
    shutil.copyfile(ROOT / "scripts/smoke_release_installer.py", installer_smoke)
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(installer_smoke),
            str(extracted),
            version,
            sha,
        ],
        cwd=scratch,
        check=True,
        timeout=60,
    )
    venv = scratch / "venv"
    run(sys.executable, "-m", "venv", str(venv))
    python = str(venv / "bin/python")
    run(
        python,
        "-I",
        "-m",
        "pip",
        "--isolated",
        "install",
        "--require-hashes",
        "--only-binary=:all:",
        "-r",
        str(extracted / "runtime-requirements.txt"),
    )
    run(
        python,
        "-I",
        "-m",
        "pip",
        "--isolated",
        "install",
        "--no-deps",
        str(extracted / wheel_name),
    )
    run(python, "-I", "-m", "pip", "check")
    smoke = scratch / "smoke_installed.py"
    shutil.copyfile(ROOT / "scripts/smoke_installed.py", smoke)
    env = dict(os.environ)
    env.pop("POSTCARDSCENE_CONFIG", None)
    subprocess.run(
        [python, "-I", str(smoke), version],
        cwd=scratch,
        env=env,
        check=True,
        timeout=120,
    )
    verify_checksums(output, (archive_name, wheel_name))


def build(output, require_tag=True):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    sha, timestamp = source_identity(project, require_tag)
    if run("uv", "--version", capture_output=True, text=True).stdout.split()[:2] != [
        "uv",
        "0.12.10",
    ]:
        raise ValueError("Release build requires uv 0.12.10")
    exported = run(
        "uv",
        "export",
        "--format",
        "requirements.txt",
        "--locked",
        "--no-dev",
        "--no-emit-project",
        "--no-sources",
        "--no-header",
        capture_output=True,
    )
    if exported.stdout != (ROOT / "runtime-requirements.txt").read_bytes():
        raise ValueError("Stale runtime requirements")
    output.mkdir(parents=True, exist_ok=False)
    version = project["version"]
    with tempfile.TemporaryDirectory(prefix="postcardscene-artifact-") as temporary:
        scratch = Path(temporary)
        env = dict(os.environ, SOURCE_DATE_EPOCH=str(timestamp))
        run("uv", "build", "--wheel", "--no-sources", "--out-dir", str(output), env=env)
        wheel = output / f"postcardscene-{version}-py3-none-any.whl"
        check_wheel(wheel, project)
        bundle = scratch / "bundle"
        manifest = prepare_bundle(bundle, wheel, version, sha)
        archive = output / f"postcardscene-{version}-linux-native.tar.gz"
        write_archive(bundle, archive, timestamp)
        write_checksums(output, (archive.name, wheel.name))
        smoke_archive(output, project, scratch, sha)
    (output / "release-notes.md").write_text(
        f"# PostcardScene {version}\n\nSource: `{sha}`; tag: `v{version}`.\n\n"
        "This candidate supplies the native install/remove bundle, external checksums "
        "and an extracted-member integrity gate. Compatible reinstall preserves config, "
        "database/admin state, signing key and service identities.\n\n"
        f"Packaged schema: `{manifest['schema']['alembic_head']}`. "
        "Clean installation explicitly initializes the schema; reinstall does not migrate. "
        "Forward update and backup/restore remain unavailable.\n\n"
        "Managed targets remain ARM64 Ubuntu Server 24.04/26.04 and Raspberry Pi OS "
        "64-bit / Debian 13 Trixie, with CPython 3.11–3.14. Generic artifact smoke "
        "does not prove physical Pi/HDMI support or complete V0 security/recovery gates.\n\n"
        "Verify SHA256SUMS from this GitHub Release before executing extracted installer code. "
        "See docs/operations/installation.md at the exact tag for install/remove instructions. "
        "Different repacked bytes require new checksums and candidate evidence.\n"
    )
    if "GITHUB_OUTPUT" in os.environ:
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output_file:
            output_file.write(f"prerelease={github_prerelease(version)}\n")
    print(f"Final native archive smoke passed: {archive.name}; source {sha}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.output.resolve())
