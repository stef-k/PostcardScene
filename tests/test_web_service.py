"""Packaged service policy and foreground installed-layout process evidence."""

import os
import signal
import socket
import subprocess
import sys
import time
from configparser import ConfigParser
from http.client import HTTPConnection
from importlib.resources import files
from pathlib import Path

import pytest

from postcardscene.schema import upgrade_database
from postcardscene.session_secret import initialize_secret


def test_packaged_web_service_contract_and_independent_lifetimes():
    assets = files("postcardscene.web").joinpath("systemd")
    assert [asset.name for asset in assets.iterdir()] == ["postcardscene-web.service"]
    unit = ConfigParser(interpolation=None)
    unit.optionxform = str
    unit.read_string(assets.joinpath("postcardscene-web.service").read_text())
    assert {section: dict(unit[section]) for section in unit.sections()} == {
        "Unit": {
            "Description": "PostcardScene control plane",
            "StartLimitIntervalSec": "60",
            "StartLimitBurst": "5",
        },
        "Service": {
            "Type": "exec",
            "User": "postcardscene-web",
            "Group": "postcardscene",
            "Environment": "POSTCARDSCENE_CONFIG=/etc/postcardscene/config.py",
            "ExecStart": "/opt/postcardscene/venv/bin/postcardscene-web",
            "Restart": "on-failure",
            "RestartSec": "5",
            "TimeoutStartSec": "15",
            "TimeoutStopSec": "30",
            "KillMode": "control-group",
            "SendSIGKILL": "yes",
            "StandardOutput": "journal",
            "StandardError": "journal",
            "NoNewPrivileges": "yes",
            "CapabilityBoundingSet": "",
            "AmbientCapabilities": "",
            "PrivateDevices": "yes",
            "DevicePolicy": "closed",
            "ProtectSystem": "strict",
            "ProtectHome": "yes",
            "ReadWritePaths": "/var/lib/postcardscene",
            "InaccessiblePaths": "-/run/postcardscene-wayland",
            "PrivateTmp": "yes",
            "RestrictAddressFamilies": "AF_UNIX AF_INET AF_INET6",
        },
        "Install": {"WantedBy": "multi-user.target"},
    }
    # Neither other service has a reverse dependency that stops/restarts web.
    for owner in ("runtime", "graphics"):
        text = (
            files(f"postcardscene.{owner}")
            .joinpath(f"systemd/postcardscene-{owner}.service")
            .read_text()
        )
        assert "postcardscene-web" not in text


@pytest.mark.parametrize("mode", ["direct_http", "reverse_proxy_https"])
def test_foreground_web_serves_layout_and_stops_on_term(tmp_path, mode):
    # Model installed roots using this test UID; real two-UID DAC and systemd
    # namespaces require a provisioned host and are not claimed by this test.
    state = tmp_path / "postcardscene"
    state.mkdir(mode=0o770)
    database = state / "postcardscene.sqlite3"
    upgrade_database(database)
    private = tmp_path / "postcardscene-web"
    private.mkdir(mode=0o700)
    key = private / "session.key"
    initialize_secret(key)
    before = (database.read_bytes(), key.read_bytes())
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    config = tmp_path / "config.py"
    config.write_text(
        f"DATABASE_PATH = {str(database)!r}\n"
        f"SESSION_SECRET_PATH = {str(key)!r}\n"
        f"WEB_TRANSPORT_MODE = {mode!r}\n"
        f"WEB_BIND_PORT = {port}\n"
        "TRUSTED_HOSTS = ['localhost']\n"
    )
    process = subprocess.Popen(
        [str(Path(sys.executable).with_name("postcardscene-web"))],
        env={**os.environ, "POSTCARDSCENE_CONFIG": str(config)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    headers = {"Host": "localhost"}
    if mode == "reverse_proxy_https":
        headers.update({"X-Forwarded-Proto": "https", "X-Forwarded-Host": "localhost"})
    try:
        deadline = time.monotonic() + 10
        while True:
            connection = HTTPConnection("127.0.0.1", port, timeout=1)
            try:
                connection.request(
                    "GET", "/login?secret=PRIVATE-CREDENTIAL", headers=headers
                )
                response = connection.getresponse()
                assert response.status == 200
                assert b"csrf_token" in response.read()
                break
            except OSError:
                assert process.poll() is None
                assert time.monotonic() < deadline
                time.sleep(0.05)
            finally:
                connection.close()
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == -signal.SIGTERM
        assert stdout == b""
        assert stderr.startswith(b"Web server starting.\n")
        assert set(stderr.splitlines()) <= {
            b"Web server starting.",
            b"Web server diagnostic.",
        }
        assert (database.read_bytes(), key.read_bytes()) == before
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
