"""Run with the fresh wheel environment's Python in isolated mode (-I)."""

import base64
import hashlib
import importlib.metadata
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import postcardscene
import postcardscene.runtime
from postcardscene.session_secret import initialize_secret
from postcardscene.web import create_app


def main():
    expected = sys.argv[1]
    distribution = importlib.metadata.distribution("postcardscene")
    assert distribution.version == expected
    assert Path(postcardscene.__file__).is_relative_to(Path(sys.prefix))
    # Compare every installed payload/asset to its wheel RECORD, via pip's install.
    for entry in distribution.files:
        assert entry.locate().is_file(), entry
        if entry.hash is not None:
            digest = hashlib.new(entry.hash.mode, entry.locate().read_bytes()).digest()
            assert (
                base64.urlsafe_b64encode(digest).decode().rstrip("=")
                == entry.hash.value
            ), entry
    with tempfile.TemporaryDirectory(prefix="postcardscene-smoke-") as directory:
        root = Path(directory)
        key = root / "session.key"
        initialize_secret(key)
        app = create_app(
            {
                "TESTING": True,
                "LOGIN_DISABLED": True,
                "SESSION_SECRET_PATH": key,
                "DATABASE_PATH": root / "absent.sqlite3",
            }
        )
        try:
            client = app.test_client()
            overview = client.get("/")
            assert overview.status_code == 200
            assert f"Version {expected}" in overview.text
            assert client.get("/static/control.css").status_code == 200
        finally:
            app.extensions["postcardscene.database"].engine.dispose()
        # Entry points must import and fail promptly with missing host authority.
        # No service, schema initialization, graphics session or socket is started.
        env = dict(os.environ, POSTCARDSCENE_CONFIG=str(root / "missing-config.py"))
        for entry in distribution.entry_points:
            if entry.group == "console_scripts":
                assert callable(entry.load())
                result = subprocess.run(
                    [str(Path(sys.executable).parent / entry.name)],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    timeout=10,
                )
                expected_exit = (
                    2
                    if entry.name in {"postcardscene-doctor", "postcardscene-backup"}
                    else 1
                )
                assert result.returncode == expected_exit, (entry.name, result.stderr)
        assert not (root / "absent.sqlite3").exists()
    print(f"Installed package and Overview agree: {expected}")


if __name__ == "__main__":
    main()
