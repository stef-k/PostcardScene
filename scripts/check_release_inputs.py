"""Check locked export and a fresh binary-only wheel install; no managed host setup."""

import configparser
import email.parser
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, timeout=300, **kwargs)


def check_wheel(path, project):
    version = project["version"]
    assert path.name == f"postcardscene-{version}-py3-none-any.whl", path.name
    info = f"postcardscene-{version}.dist-info"
    tracked = run("git", "ls-files", "-z", "src/postcardscene", capture_output=True)
    payload = {
        name.removeprefix("src/"): ROOT / name
        for name in tracked.stdout.decode().split("\0")
        if name
    }
    metadata_files = {
        f"{info}/{name}"
        for name in (
            "METADATA",
            "WHEEL",
            "RECORD",
            "entry_points.txt",
            "licenses/LICENSE",
        )
    }
    with zipfile.ZipFile(path) as wheel:
        files = [entry.filename for entry in wheel.infolist() if not entry.is_dir()]
        assert len(files) == len(set(files)), "Duplicate wheel members"
        assert set(files) == payload.keys() | metadata_files, (
            "Unexpected/missing payload"
        )
        for name, source in payload.items():
            assert wheel.read(name) == source.read_bytes(), name
        metadata = email.parser.BytesParser().parsebytes(wheel.read(f"{info}/METADATA"))
        assert metadata["Name"] == project["name"]
        assert metadata["Version"] == version
        assert set(metadata["Requires-Python"].replace(" ", "").split(",")) == set(
            project["requires-python"].split(",")
        )
        tags = email.parser.BytesParser().parsebytes(wheel.read(f"{info}/WHEEL"))
        assert tags["Root-Is-Purelib"] == "true"
        assert tags.get_all("Tag") == ["py3-none-any"]
        entries = configparser.ConfigParser()
        entries.read_string(wheel.read(f"{info}/entry_points.txt").decode())
        assert dict(entries["console_scripts"]) == project["scripts"]


def main():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    with tempfile.TemporaryDirectory(prefix="postcardscene-release-") as directory:
        scratch = Path(directory)
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
        assert exported.stdout == (ROOT / "runtime-requirements.txt").read_bytes(), (
            "runtime-requirements.txt is stale; regenerate using the documented uv export"
        )
        run("uv", "build", "--wheel", "--no-sources", "--out-dir", str(scratch))
        wheels = list(scratch.glob("*.whl"))
        assert len(wheels) == 1, "Expected exactly one application wheel"
        check_wheel(wheels[0], project)
        for name in ("install.py", "install_preflight.py", "runtime-requirements.txt"):
            shutil.copyfile(ROOT / name, scratch / name)
        run(
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import pathlib, runpy; "
            f"p=pathlib.Path({str(scratch)!r}); "
            "installer=runpy.run_path(str(p / 'install.py')); "
            "installer['validate_inputs'](p)",
        )
        venv = scratch / "venv"
        run(sys.executable, "-m", "venv", str(venv))
        python = str(venv / "bin/python")
        run(
            python,
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "--only-binary=:all:",
            "-r",
            str(ROOT / "runtime-requirements.txt"),
        )
        run(python, "-m", "pip", "install", "--no-deps", str(wheels[0]))
        run(python, "-m", "pip", "check")
        env = dict(os.environ)
        env.pop("POSTCARDSCENE_CONFIG", None)
        run(
            python,
            "-I",
            str(ROOT / "scripts/smoke_installed.py"),
            project["version"],
            env=env,
        )
    print("Release inputs and isolated installed wheel smoke passed.")


if __name__ == "__main__":
    main()
