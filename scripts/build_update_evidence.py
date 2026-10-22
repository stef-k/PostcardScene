"""CI-only predecessor/candidate artifacts; never publish or tag the predecessor."""

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from build_release import build
from release_bundle import ROOT

BASELINE = "fde2cd540220e53c19d726147aa8956f45ce0d91"


def backport_security_tooling(source):
    """Keep the synthetic predecessor's downloads safe without upgrading its app."""
    current = (ROOT / "install_host.py").read_text()
    function = next(
        node
        for node in ast.parse(current).body
        if isinstance(node, ast.FunctionDef) and node.name == "bootstrap_pip"
    )
    helper = ast.get_source_segment(current, function)
    path = source / "install_host.py"
    old = path.read_text()
    seed = '    pip = (python, "-I", "-m", "pip", "--isolated", "--disable-pip-version-check")'
    assert old.count(seed) == 1
    old = old.replace(seed, "    pip = bootstrap_pip(python, run)")
    path.write_text(
        old.replace("def stage_payload(", helper + "\n\n\ndef stage_payload(")
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path = source / "install.py"
    old = path.read_text()
    pin = next(line for line in old.splitlines() if '"install_host.py":' in line)
    path.write_text(old.replace(pin, f'    "install_host.py": "{digest}",'))
    path = source / "scripts/build_release.py"
    old = path.read_text().replace("0.12.10", "0.13.5")
    old = old.replace("import re\n", "import re\nimport runpy\n")
    marker = '    python = str(venv / "bin/python")\n'
    assert old.count(marker) == 1
    path.write_text(
        old.replace(
            marker,
            marker
            + (
                '    installer = runpy.run_path(str(extracted / "install.py"))\n'
                '    host = installer["validate_inputs"](extracted)[-1]\n'
                "    host.bootstrap_pip(python, lambda args, **kwargs: run(*args, **kwargs))\n"
            ),
        )
    )


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

        # A shallow CI checkout may have the baseline only in FETCH_HEAD; a
        # clone does not promise to transfer objects unreachable from its refs.
        run("git", "fetch", "--no-tags", "origin", BASELINE)
        run("git", "checkout", "--detach", BASELINE)
        project = source / "pyproject.toml"
        original = project.read_text()
        assert original.count('version = "0.1.0.dev0"') == 1
        project.write_text(
            original.replace('version = "0.1.0.dev0"', 'version = "0.0.0"')
        )
        backport_security_tooling(source)
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
        # Runtime dependencies stay frozen; only installer/tooling trust changes.
        assert exported == (source / "runtime-requirements.txt").read_bytes()
        changed = run("git", "diff", "--name-only", capture_output=True, text=True)
        expected = {
            "pyproject.toml",
            "uv.lock",
            "install.py",
            "install_host.py",
            "scripts/build_release.py",
        }
        assert set(changed.stdout.splitlines()) == expected
        run("git", "add", *sorted(expected))
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
        # The baseline builder retains its product smoke and the explicit trust backport.
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
        "method": "local unpushed commit; security-backported clean-tree builder require_tag=False",
        "security_backport": [
            "uv 0.13.5",
            "hash-pinned pip 26.2.2",
            "installer host pin",
        ],
        "limitation": "same-schema update; ancestor migration uses separate installed-target fixture",
    }
    (output / "provenance.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence), flush=True)


if __name__ == "__main__":
    main()
