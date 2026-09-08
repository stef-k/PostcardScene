import json
import os
import socket
from threading import Event
from time import monotonic

import pytest

from postcardscene.power import Kind, PowerResult, Reason, Status
from postcardscene.runtime.panel import PanelCoordinator
from postcardscene.runtime.panel_control import PanelControl


class UnavailableBackend:
    kind = Kind.SIGNAL

    def probe(self, cancelled):
        return PowerResult(self.kind, Status.UNAVAILABLE, Reason.UNAVAILABLE)


@pytest.fixture
def control(tmp_path):
    tmp_path.chmod(0o700)
    backend = UnavailableBackend()
    owner = PanelCoordinator(None, Event(), cec=backend, ddc=backend, signal=backend)
    server = PanelControl(owner, owner.stop_event, tmp_path / "panel.sock")
    server.start()
    yield server, owner
    server.stop()


def exchange(server, request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
        client.settimeout(1)
        client.connect(str(server.path))
        client.sendall(request)
        response = client.recv(1025)
        assert len(response) <= 1024
        return json.loads(response)


def test_status_tests_and_protection_use_coordinator_only(control):
    server, owner = control
    response = exchange(server, b'{"version":1,"action":"status"}')
    assert response["status"]["physical"] == "unknown"
    assert response["status"]["configured"] is True
    assert (
        exchange(server, b'{"version":1,"action":"test_off"}')["outcome"] == "accepted"
    )
    assert owner.status.diagnostic_active is False
    owner.set_protection(True)
    assert (
        exchange(server, b'{"version":1,"action":"test_on"}')["outcome"] == "rejected"
    )
    assert owner.status.protection_sleep
    assert owner.status.diagnostic_active is False
    owner._clock = lambda: float("inf")
    owner._expire_test()
    assert owner.status.diagnostic_active is None
    assert owner.status.protection_sleep


@pytest.mark.parametrize(
    "raw",
    [
        b"x" * 1025,
        b"{",
        b"[]",
        b'{"version":true,"action":"test_on"}',
        b'{"version":2,"action":"test_on"}',
        b'{"version":1,"action":"test_on","path":"secret"}',
        b'{"version":1,"action":"power_on"}',
        b'{"version":1,"action":{}}',
        b"\xff",
    ],
)
def test_rejected_packets_never_submit_intent(control, raw):
    server, owner = control
    response = exchange(server, raw)
    assert response["outcome"] == "rejected"
    assert owner.status.diagnostic_active is None
    assert "secret" not in json.dumps(response)


def test_unauthorized_peer_and_private_endpoint(control, monkeypatch):
    server, owner = control
    assert server.path.stat().st_mode & 0o777 == 0o600
    assert server.path.stat().st_uid == os.geteuid()
    monkeypatch.setattr(server, "_authorized", lambda client: False)
    assert (
        exchange(server, b'{"version":1,"action":"test_on"}')["outcome"] == "rejected"
    )
    assert owner.status.diagnostic_active is None


def test_silent_client_shutdown_is_bounded_and_removes_only_owned_socket(control):
    server, _ = control
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
        client.connect(str(server.path))
        start = monotonic()
        server.stop()
        assert monotonic() - start < 1
    assert not server.path.exists()
    assert not server.thread.is_alive()


def test_directory_and_existing_endpoint_authority(tmp_path):
    path = tmp_path / "panel.sock"
    tmp_path.chmod(0o777)
    server = PanelControl(None, Event(), path)
    with pytest.raises(ValueError):
        server.start()
    tmp_path.chmod(0o700)
    path.write_text("foreign")
    with pytest.raises(OSError):
        server.start()
    server.stop()
    assert path.read_text() == "foreign"


def test_unavailable_status(tmp_path):
    tmp_path.chmod(0o700)
    server = PanelControl(None, Event(), tmp_path / "panel.sock")
    server.start()
    try:
        response = exchange(server, b'{"version":1,"action":"test_on"}')
        assert response["outcome"] == "unavailable"
        assert response["status"]["configured"] is False
    finally:
        server.stop()
