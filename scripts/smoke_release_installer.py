"""Non-mutating validation of an externally verified extracted release candidate."""

import json
import runpy
import subprocess
import sys
from pathlib import Path


def main():
    bundle = Path(sys.argv[1])
    version, sha = sys.argv[2:]
    installer = runpy.run_path(str(bundle / "install.py"))
    assert installer["validate_inputs"](bundle)[0] == version
    manifest_path = bundle / "release-manifest.json"
    original = manifest_path.read_bytes()
    manifest = json.loads(original)
    assert manifest["version"] == version
    assert manifest["tag"] == "v" + version
    assert manifest["source_commit"] == sha
    # Deliberately corrupt only the disposable extraction, then restore exact bytes.
    # The pinned input validator may run; host/preflight code and mutation may not.
    util = installer["importlib"].util
    module_from_spec = util.module_from_spec

    def only_validator(spec):
        assert spec.name == "postcardscene_install_inputs"
        return module_from_spec(spec)

    def no_mutation(*args, **kwargs):
        raise AssertionError("Host mutation during manifest rejection")

    # Every fixed helper and runtime requirement is pinned; other members are
    # authenticated by the manifest. Exercise update on the actual extracted bytes.
    for name in (
        *installer["INPUT_HASHES"],
        f"postcardscene-{version}-py3-none-any.whl",
        "release-manifest.json",
    ):
        path = bundle / name
        saved = path.read_bytes()
        try:
            path.write_bytes(saved + b"\ncorrupt")
            util.module_from_spec = only_validator
            for action in ("install", "remove", "update"):
                operation = installer["Installation"](bundle, no_mutation)
                try:
                    getattr(operation, action)()
                except (installer["InstallError"], ValueError):
                    pass
                else:
                    raise AssertionError("Damaged member accepted: " + name)
                assert operation.host is None
        finally:
            util.module_from_spec = module_from_spec
            path.write_bytes(saved)
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(bundle / "install.py"), "--help"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "{install,remove,update}" in result.stdout
    print("Extracted installer identity, pins and pre-mutation manifest gate passed.")


if __name__ == "__main__":
    main()
