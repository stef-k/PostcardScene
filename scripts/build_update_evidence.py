"""CI-only predecessor/candidate artifacts; never publish or tag the predecessor."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from build_release import build
from release_bundle import ROOT

BASELINE = "fde2cd540220e53c19d726147aa8956f45ce0d91"


def predecessor(output):
    # An independent temporary repository cannot alter checkout refs or history.
    with tempfile.TemporaryDirectory(prefix="postcardscene-predecessor-") as temp:
        source = Path(temp) / "source"
        subprocess.run(
            ("git", "clone", "--no-hardlinks", "--no-checkout", str(ROOT), str(source)),
            check=True,
        )

        def run(*args, **kwargs):
            return subprocess.run(args, cwd=source, check=True, timeout=300, **kwargs)

        run("git", "checkout", "--detach", BASELINE)
        project = source / "pyproject.toml"
        original = project.read_text()
        assert original.count('version = "0.1.0.dev0"') == 1
        project.write_text(
            original.replace('version = "0.1.0.dev0"', 'version = "0.0.0"')
        )
        run("uv", "lock")
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
        ).stdout
        # No dependencies changed: the frozen bootstrap pins remain exact.
        assert exported == (source / "runtime-requirements.txt").read_bytes()
        changed = run("git", "diff", "--name-only", capture_output=True, text=True)
        assert set(changed.stdout.splitlines()) == {"pyproject.toml", "uv.lock"}
        run("git", "add", "pyproject.toml", "uv.lock")
        run(
            "git",
            "-c",
            "user.name=PostcardScene CI",
            "-c",
            "user.email=ci@example.invalid",
            "commit",
            "-m",
            "CI-only synthetic predecessor 0.0.0; never publish",
        )
        sha = run(
            "git", "rev-parse", "HEAD", capture_output=True, text=True
        ).stdout.strip()
        assert (
            run(
                "git", "rev-parse", "HEAD^", capture_output=True, text=True
            ).stdout.strip()
            == BASELINE
        )
        # The baseline's ordinary builder and smoke remain unchanged and authoritative.
        run(
            sys.executable,
            "-B",
            "-c",
            "import sys; from pathlib import Path; sys.path.insert(0, 'scripts'); "
            "from build_release import build; build(Path(sys.argv[1]), require_tag=False)",
            str(output),
            env={k: v for k, v in os.environ.items() if k != "GITHUB_OUTPUT"},
        )
        return sha


def main():
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=False)
    sha = predecessor(output / "predecessor")
    build(output / "candidate", require_tag=False)
    evidence = {
        "kind": "CI-only synthetic predecessor; not a supported or published release",
        "baseline": BASELINE,
        "synthetic_commit": sha,
        "version": "0.0.0",
        "method": "local unpushed commit; normal clean-tree builder require_tag=False",
        "limitation": "same-schema update; ancestor migration uses separate installed-target fixture",
    }
    (output / "provenance.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence), flush=True)


if __name__ == "__main__":
    main()
