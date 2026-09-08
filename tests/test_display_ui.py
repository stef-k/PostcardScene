"""Display forms and closed panel transport at the HTTP/socket boundaries."""

import json
import re
import socket
from dataclasses import asdict
from threading import Thread

import pytest

from postcardscene import panel_client
from postcardscene.operating_schedule import get_operating_schedule
from postcardscene.runtime.panel import PanelStatus
from postcardscene.settings import DisplayPowerSettings, get_display_power_settings


@pytest.fixture
def panel_peer(tmp_path, monkeypatch):
    path = str(tmp_path / "panel.sock")
    monkeypatch.setattr(panel_client, "SOCKET_PATH", path)
    requests = []
    response = {
        "version": 1,
        "outcome": "accepted",
        "status": {
            "configured": True,
            **asdict(PanelStatus()),
            "intentional_signal_sleep": False,
        },
    }

    def exchange(callback, payload=None):
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as server:
            server.bind(path)
            server.listen(1)
            server.settimeout(2)

            def serve():
                with server.accept()[0] as peer:
                    peer.settimeout(1)
                    requests.append(json.loads(peer.recv(1024)))
                    peer.sendall(
                        json.dumps(response).encode() if payload is None else payload
                    )

            thread = Thread(target=serve)
            thread.start()
            try:
                return callback()
            finally:
                thread.join(3)
                assert not thread.is_alive()
                from pathlib import Path

                Path(path).unlink()

    return response, requests, exchange


@pytest.fixture
def display_ui(sources_ui, monkeypatch, tmp_path):
    app, client, db, _, _ = sources_ui
    monkeypatch.setattr(panel_client, "SOCKET_PATH", str(tmp_path / "absent.sock"))
    token = re.search(
        r'name="csrf_token"[^>]*value="([^"]+)"', client.get("/display").text
    )[1]

    def post(path="/display", **data):
        return client.post(path, data={"csrf_token": token, **data})

    return app, client, db, post


def test_policy_roundtrip_unavailable_and_atomic_validation(display_ui, monkeypatch):
    _, client, db, post = display_ui

    def forbidden(*args, **kwargs):
        pytest.fail("Display request started hardware, network or background work")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    monkeypatch.setattr("threading.Thread.start", forbidden)
    assert "Runtime panel status unavailable" in client.get("/display").text
    for backend, wake, dwell in (
        ("cec", 0, 300),
        ("ddc", 30, 14400),
        ("signal", 5, 1800),
        ("auto", 7, 600),
    ):
        data = dict(
            display_power_backend=backend,
            display_wake_delay_seconds=wake,
            maximum_static_dwell_seconds=dwell,
        )
        # A successful save does not even read live status before redirecting.
        with monkeypatch.context() as patch:
            patch.setattr(panel_client, "read_status", forbidden)
            assert post(**data).status_code == 302
        assert get_display_power_settings(db) == DisplayPowerSettings(
            backend, wake, dwell
        )
        page = client.get("/display").text
        assert f'value="{backend}"' in page and f'value="{wake}"' in page
        assert f"Configured maximum static dwell: {dwell} seconds" in page
    before = get_display_power_settings(db)
    for changes in (
        {"display_power_backend": "disabled"},
        {"display_wake_delay_seconds": "31"},
        {"display_wake_delay_seconds": "-1"},
        {"display_wake_delay_seconds": "1.5"},
        {"maximum_static_dwell_seconds": "299"},
        {"maximum_static_dwell_seconds": "14401"},
        {"maximum_static_dwell_seconds": ""},
    ):
        assert 'aria-invalid="true"' in post(**{**data, **changes}).text
        assert get_display_power_settings(db) == before
    for explanation in (
        "strict",
        "HDMI-CEC → DDC/CI → signal",
        "actual wake transition",
        "cannot be disabled",
        "Pause and schedule Keep active",
        "not implemented yet",
    ):
        assert explanation in page


def test_auth_csrf_and_fixed_actions(display_ui, panel_peer):
    app, client, db, post = display_ui
    response, requests, exchange = panel_peer
    anonymous = app.test_client()
    token = re.search(
        r'name="csrf_token"[^>]*value="([^"]+)"', anonymous.get("/login").text
    )[1]
    assert anonymous.get("/display").location.startswith("/login")
    before = (get_display_power_settings(db), get_operating_schedule(db))
    for path in ("/display", "/display/test-wake", "/display/test-sleep"):
        assert anonymous.post(path, data={"csrf_token": token}).location.startswith(
            "/login"
        )
        for csrf in (None, "bad"):
            assert client.post(path, data={"csrf_token": csrf}).status_code == 400
    for path, action in (
        ("/display/test-wake", "test_on"),
        ("/display/test-sleep", "test_off"),
    ):
        assert client.get(path).status_code == 405
        assert (
            exchange(
                lambda: post(path, action="arbitrary", duration=999, backend="ddc")
            ).status_code
            == 302
        )
        assert requests[-1] == {"version": 1, "action": action}
    assert len(requests) == 2
    assert (get_display_power_settings(db), get_operating_schedule(db)) == before
    assert "does not confirm a power transition" in client.get("/display").text
    for outcome, message in (
        ("rejected", "cannot override panel protection"),
        ("cleanup_failed", "runtime recovery is required"),
        ("unavailable", "could not be confirmed"),
    ):
        response["outcome"] = outcome
        exchange(lambda: post("/display/test-wake"))
        assert message in client.get("/display").text


def test_physical_signal_and_degraded_status(display_ui, panel_peer):
    _, client, _, _ = display_ui
    response, requests, exchange = panel_peer
    status = response["status"]
    status.update(
        state="ready",
        active_backend="cec",
        physical="off",
        evidence="physical",
        reason="confirmed",
    )
    page = exchange(lambda: client.get("/display")).text
    assert "Physical state confirmed: off" in page
    status.update(
        active_backend="signal",
        physical="unknown",
        signal="off",
        evidence="signal_only",
        reason="signal_only",
    )
    page = exchange(lambda: client.get("/display")).text
    assert "Signal output off; physical panel unknown" in page
    assert "Physical state confirmed" not in page
    status.update(
        state="degraded", evidence="none", signal="unknown", reason="tool_missing"
    )
    assert "Unknown/degraded" in exchange(lambda: client.get("/display")).text
    assert requests == [{"version": 1, "action": "status"}] * 3


@pytest.mark.parametrize(
    "payload",
    [
        b"broken /private/path",
        b"x" * 1025,
        b"[]",
        b'{"version":true,"outcome":"accepted","status":{}}',
    ],
)
def test_bad_protocol_is_unavailable(panel_peer, payload):
    _, _, exchange = panel_peer
    assert exchange(panel_client.read_status, payload) == panel_client.PanelResponse()


def test_untrusted_status_never_reaches_page(display_ui, panel_peer):
    _, client, _, _ = display_ui
    response, _, exchange = panel_peer
    response["status"]["reason"] = "/private/device raw error"
    page = exchange(lambda: client.get("/display")).text
    assert "Runtime panel status unavailable" in page
    assert "/private/device" not in page
    response["status"] = {
        "configured": False,
        "state": "unavailable",
        "reason": "not_configured",
    }
    assert (
        "Runtime panel status unavailable"
        in exchange(lambda: client.get("/display")).text
    )
