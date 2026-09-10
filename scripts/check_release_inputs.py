"""Check locked export and a fresh binary-only wheel install; no managed host setup."""

import configparser
import email.parser
import subprocess
import tempfile
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
    from build_release import build

    with tempfile.TemporaryDirectory(prefix="postcardscene-release-") as directory:
        build(Path(directory) / "artifacts", require_tag=False)


if __name__ == "__main__":
    main()
