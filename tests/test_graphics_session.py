"""Session authority and readiness at the Unix socket / exec boundary."""

import os
import socket
import struct
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from threading import Thread

import pytest

from postcardscene.graphics import WaylandSession, cli


@pytest.fixture
def session(tmp_path):
    tmp_path.chmod(0o700)
    return WaylandSession(tmp_path)


@contextmanager
def compositor(session, reply):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(session.socket_path))
        server.listen()
        server.settimeout(2)
        errors = []

        def respond():
            try:
                with server.accept()[0] as client:
                    client.settimeout(2)
                    request = bytearray()
                    while len(request) < 12:
                        chunk = client.recv(12 - len(request))
                        if not chunk:  # Peer rejection closes before sync.
                            return
                        request.extend(chunk)
                    assert request == struct.pack("=III", 1, 12 << 16, 2)
                    if reply is None:
                        time.sleep(0.4)
                    else:
                        for byte in reply:  # Exercise stream fragmentation.
                            client.sendall(bytes([byte]))
            except Exception as error:
                errors.append(error)

        thread = Thread(target=respond)
        thread.start()
        try:
            yield
        finally:
            thread.join(3)
            assert not thread.is_alive()
            assert not errors


def test_responsive_wayland_and_explicit_environment(session, monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "must-not-escape")
    monkeypatch.setenv("DISPLAY", ":99")
    with compositor(session, struct.pack("=III", 2, 12 << 16, 123)):
        status = session.inspect(compositor_pid=os.getpid())
    assert status.available and status.compositor_state == "running"
    environment = session.client_environment()
    assert environment["XDG_RUNTIME_DIR"] == str(session.runtime_directory)
    assert environment["WAYLAND_DISPLAY"] == "wayland-0"
    assert environment["GDK_BACKEND"] == environment["QT_QPA_PLATFORM"] == "wayland"
    assert "SECRET_TOKEN" not in environment and "DISPLAY" not in environment
    assert "must-not-escape" not in str(status.public_diagnostics())
    assert str(session.runtime_directory) not in str(status.public_diagnostics())


@pytest.mark.parametrize("reply", [None, b"", struct.pack("=III", 1, 12 << 16, 0)])
def test_listening_socket_alone_is_not_ready(session, reply):
    with compositor(session, reply):
        started = time.monotonic()
        status = session.inspect()
        assert time.monotonic() - started < 1
    assert not status.available
    assert status.reason == "wayland_unresponsive"


def test_wrong_compositor_pid_is_not_ready(session):
    with compositor(session, struct.pack("=III", 2, 12 << 16, 0)):
        status = session.inspect(compositor_pid=os.getppid())
    assert not status.available and status.reason == "unexpected_peer"


def test_missing_stale_and_non_socket_are_unavailable(session):
    assert not session.inspect().available
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
        stale.bind(str(session.socket_path))
    assert not session.inspect().available
    session.socket_path.unlink()
    session.socket_path.write_text("not a socket")
    assert session.inspect().reason == "invalid_socket"


def test_directory_permissions_owner_and_symlinks(session, tmp_path, monkeypatch):
    session.runtime_directory.chmod(0o755)
    assert not session.inspect().runtime_directory_valid
    session.runtime_directory.chmod(0o700)
    link = tmp_path / "alias"
    link.symlink_to(session.runtime_directory, target_is_directory=True)
    assert not WaylandSession(link).inspect().runtime_directory_valid
    with monkeypatch.context() as patch:
        patch.setattr(os, "geteuid", lambda: 0)
        assert not session.inspect().runtime_directory_valid
    monkeypatch.setattr(
        os, "geteuid", lambda: session.runtime_directory.stat().st_uid + 1
    )
    assert not session.inspect().runtime_directory_valid


@pytest.mark.parametrize("path", ["relative", "/run/../tmp", "/" + "a" * 110])
def test_invalid_session_path_is_rejected(path):
    with pytest.raises(ValueError):
        WaylandSession(path)


@pytest.mark.parametrize("backend", ["logind", "seatd"])
def test_same_exec_contract_for_both_seat_provisioning_paths(
    session, monkeypatch, backend
):
    calls = []
    monkeypatch.setenv("LIBSEAT_BACKEND", backend)
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setenv("WAYLAND_SOCKET", "3")
    monkeypatch.setenv("LD_PRELOAD", "secret")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "secret")
    monkeypatch.setattr(os, "execve", lambda *args: calls.append(args))
    cli.launch(session)
    executable, argv, environment = calls[0]
    assert executable == "/usr/bin/labwc"
    assert argv == ["labwc", "-C", str(cli.ASSETS / "labwc")]
    assert environment["LIBSEAT_BACKEND"] == backend
    assert environment["WLR_BACKENDS"] == "drm,libinput"
    assert environment["WLR_XWAYLAND"] == "/usr/bin/false"
    assert environment["XKB_DEFAULT_OPTIONS"] == "srvrkeys:none"
    assert (
        not {
            "DISPLAY",
            "WAYLAND_DISPLAY",
            "WAYLAND_SOCKET",
            "LD_PRELOAD",
            "DBUS_SESSION_BUS_ADDRESS",
        }
        & environment.keys()
    )


def test_launch_refuses_foreign_socket_and_missing_binary(session, monkeypatch):
    session.socket_path.write_text("foreign")
    with pytest.raises(ValueError):
        cli.launch(session)
    session.socket_path.unlink()

    def missing(*args):
        raise FileNotFoundError

    monkeypatch.setattr(os, "execve", missing)
    with pytest.raises(FileNotFoundError):
        cli.launch(session)
    assert not session.inspect().available


def test_exited_compositor_fails_readiness_immediately(session, monkeypatch, capsys):
    def exited(*args):
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", exited)
    assert cli.wait_ready(session, 123) == 1
    assert "compositor_exited" in capsys.readouterr().err


def test_readiness_deadline_is_bounded(session, monkeypatch, capsys):
    ticks = iter([0, 11])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(ticks))
    assert cli.wait_ready(session, os.getpid()) == 1
    assert "wayland_unresponsive" in capsys.readouterr().err


def test_packaged_config_suppresses_desktop_defaults():
    config = ET.parse(cli.ASSETS / "labwc/rc.xml").getroot()
    assert config.findall("./keyboard/keybind")
    assert config.findall("./mouse/context/mousebind")
    assert {action.get("name") for action in config.iter("action")} == {"None"}
    assert not config.findall(".//default")
    for name in ("autostart", "shutdown", "environment"):
        assert all(
            not line.strip() or line.startswith("#")
            for line in (cli.ASSETS / "labwc" / name).read_text().splitlines()
        )
    assert not list(ET.parse(cli.ASSETS / "labwc/menu.xml").getroot())


def test_service_owns_nonroot_lifecycle_and_bounded_cleanup():
    unit = (cli.ASSETS / "systemd/postcardscene-graphics.service").read_text()
    for directive in (
        "User=postcardscene",
        "Group=postcardscene",
        "Type=exec",
        "RuntimeDirectoryMode=0700",
        "Restart=on-failure",
        "TimeoutStartSec=15",
        "TimeoutStopSec=5",
        "KillMode=control-group",
        "SendSIGKILL=yes",
        "WantedBy=multi-user.target",
        "PAMName=postcardscene-graphics",
    ):
        assert directive in unit
    assert " wait --pid ${MAINPID}" in unit
    assert "postcardscene.runtime" not in unit
