"""Software output contract using controlled DRM, session and command boundaries."""

import json
import time
from dataclasses import FrozenInstanceError
from threading import Event, Thread

import pytest

from postcardscene.graphics import SessionStatus, WaylandSession
from postcardscene.graphics import output as policy
from postcardscene.graphics import output_probe as probe


def mode(width=1920, height=1080, refresh=60.0, preferred=True, current=True):
    return dict(
        width=width,
        height=height,
        refresh=refresh,
        preferred=preferred,
        current=current,
    )


def output(name="HDMI-A-1", modes=None, **changes):
    return dict(
        name=name,
        enabled=True,
        modes=modes if modes is not None else [mode()],
        scale=1.0,
        position={"x": 0, "y": 0},
        transform="normal",
        **changes,
    )


@pytest.fixture
def display(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "DRM_ROOT", tmp_path)
    monkeypatch.setattr(
        WaylandSession,
        "inspect",
        lambda self: SessionStatus(True, "running", True, "ready"),
    )
    environment = {"XDG_RUNTIME_DIR": "/private", "WAYLAND_DISPLAY": "wayland-0"}
    monkeypatch.setattr(WaylandSession, "client_environment", lambda self: environment)
    calls = []
    states = [[output()]]

    def command(arguments, env, stop_event=None):
        assert env is environment
        calls.append(arguments)
        return json.dumps(states[0] if len(states) == 1 else states.pop(0)).encode()

    monkeypatch.setattr(probe, "run_command", command)
    monkeypatch.setattr(policy, "run_command", command)
    return tmp_path, calls, states


def connector(root, name="card1-HDMI-A-1", connected=True, edid=b""):
    path = root / name
    path.mkdir(exist_ok=True)
    (path / "status").write_text("connected\n" if connected else "disconnected\n")
    (path / "edid").write_bytes(edid)


def test_no_display_connect_disconnect_reconnect_and_idempotence(display):
    root, calls, _ = display
    connector(root, "card0-eDP-1")
    session = WaylandSession()
    assert policy.reconcile_display(session).state == "no_display"
    assert not calls
    for connected in (True, False, True):
        connector(root, connected=connected)
        status = policy.reconcile_display(session)
        assert status.state == ("ready" if connected else "no_display")
    assert policy.reconcile_display(session).state == "ready"
    assert calls == [["--json"]] * 3
    with pytest.raises(FrozenInstanceError):
        status.state = "bad"


def test_connector_ambiguity_override_mismatch_and_duplicate_cards(display):
    root, calls, states = display
    connector(root)
    connector(root, "card1-HDMI-A-2")
    states[:] = [[output(), output("HDMI-A-2")]]
    session = WaylandSession()
    assert policy.reconcile_display(session).state == "ambiguous"
    assert not calls
    assert (
        policy.reconcile_display(session, connector_override="HDMI-A-2").connector
        == "HDMI-A-2"
    )
    for override, reason in (
        ("--off", "invalid_connector_override"),
        ("HDMI-A-3", "connector_mismatch"),
    ):
        assert (
            policy.reconcile_display(session, connector_override=override).reason
            == reason
        )
    connector(root, "card1-HDMI-A-2", connected=False)
    assert (
        policy.reconcile_display(session, connector_override="HDMI-A-2").reason
        == "connector_mismatch"
    )
    connector(root, "card2-HDMI-A-1", connected=False)
    assert policy.reconcile_display(session).reason == "connector_mismatch"


def test_wayland_mismatch_and_unavailable_session(display, monkeypatch):
    root, calls, states = display
    connector(root)
    states[:] = [[output("DP-1")]]
    assert policy.reconcile_display(WaylandSession()).reason == "connector_mismatch"
    calls.clear()
    monkeypatch.setattr(
        WaylandSession,
        "inspect",
        lambda self: SessionStatus(False, "unknown", False, "secret"),
    )
    status = policy.reconcile_display(WaylandSession())
    assert status.reason == "session_unavailable" and not status.session_available
    assert not calls and "secret" not in str(status.public_diagnostics())


@pytest.mark.parametrize(
    "modes, expected",
    [
        ([mode(), mode(3840, 2160, 59.94, False, False)], (3840, 2160, 59940)),
        ([mode(), mode(3840, 2160, 30, True, False)], (1920, 1080, 60000)),
        ([mode(1280, 720, 50)], (1280, 720, 50000)),
        ([mode(1280, 720, 50, False)], (1280, 720, 50000)),
        (
            [mode(refresh=59.94), mode(refresh=60, preferred=False, current=False)],
            (1920, 1080, 60000),
        ),
    ],
)
def test_mode_priority(modes, expected):
    assert (
        policy.choose_mode(tuple(probe.Mode(**item) for item in modes)).selector
        == expected
    )


def test_mode_ties_and_no_safe_mode():
    ordinary = probe.Mode(1920, 1080, 60)
    preferred = probe.Mode(1920, 1080, 60, preferred=True)
    current = probe.Mode(1920, 1080, 60, current=True)
    assert policy.choose_mode((ordinary, current, preferred)) is preferred
    assert policy.choose_mode((current, ordinary)) is current
    with pytest.raises(probe.ProbeError, match="no_safe_mode"):
        policy.choose_mode(
            (probe.Mode(3840, 2160, 120, True, True), probe.Mode(1280, 720, 50))
        )


def test_apply_once_verify_fresh_state_and_then_idempotent(display):
    root, calls, states = display
    connector(root)
    old = output(modes=[mode(1280, 720, 60, False), mode(current=False)])
    states[:] = [[old], [], [output()]]  # discovery, apply stdout, post-state
    assert policy.reconcile_display(WaylandSession()).state == "ready"
    assert calls == [
        ["--json"],
        [
            "--output",
            "HDMI-A-1",
            "--on",
            "--mode",
            "1920x1080@60.000Hz",
            "--pos",
            "0,0",
            "--transform",
            "normal",
            "--scale",
            "1",
        ],
        ["--json"],
    ]
    assert policy.reconcile_display(WaylandSession()).state == "ready"
    assert calls[-1] == ["--json"] and len(calls) == 4


@pytest.mark.parametrize(
    "change",
    [
        dict(scale=2),
        dict(position={"x": 10, "y": 0}),
        dict(transform="90"),
        dict(enabled=False),
    ],
)
def test_layout_and_enabled_state_are_applied_and_verified(display, change):
    root, calls, states = display
    connector(root)
    wrong = output() | change
    states[:] = [[wrong]]
    assert policy.reconcile_display(WaylandSession()).reason == "apply_failed"
    assert len(calls) == 3 and "--off" not in calls[1]


def test_ambiguous_mode_rejected_or_unique_preferred_verified(display):
    root, calls, states = display
    connector(root)
    modes = [mode(preferred=False), mode(preferred=False, current=False)]
    states[:] = [[output(modes=modes)]]
    assert policy.reconcile_display(WaylandSession()).reason == "ambiguous_mode"
    assert calls == [["--json"]]
    modes[1]["preferred"] = True
    calls.clear()
    assert policy.reconcile_display(WaylandSession()).reason == "apply_failed"
    assert "--preferred" in calls[1]
    modes[0]["current"] = False
    modes[1]["current"] = True
    calls.clear()
    assert policy.reconcile_display(WaylandSession()).state == "ready"
    assert calls == [["--json"]]


def test_safe_edid_product_identity_and_malformed_edid(display):
    root, _, states = display
    data = bytearray(128)
    data[:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    data[8:10] = ((1 << 10) | (2 << 5) | 3).to_bytes(2, "big")
    data[10:12] = b"\x34\x12"
    data[12:16] = b"SECR"
    data[127] = -sum(data) % 256
    connector(root, edid=data)
    states[0][0].update(description="SECRET", serial="SECRET", model="SECRET")
    diagnostics = policy.reconcile_display(WaylandSession()).public_diagnostics()
    assert diagnostics["manufacturer"] == "ABC" and diagnostics["product"] == "1234"
    assert "SECR" not in str(diagnostics)
    connector(root, edid=b"SECRET")
    status = policy.reconcile_display(WaylandSession())
    assert status.state == "ready" and status.manufacturer is None


@pytest.mark.parametrize(
    "raw",
    [
        b"secret",
        b"{}",
        b"[null]",
        b"[[]]",
        b'[{"name": "SECRET"}]',
        b"[" * 2000,
        json.dumps([output(modes=[mode(refresh=float("nan"))])]).encode(),
        json.dumps([output(modes=[mode(width=True)])]).encode(),
        json.dumps([output(), output()]).encode(),
    ],
)
def test_malformed_tool_output_is_sanitized(display, monkeypatch, raw):
    root, _, _ = display
    connector(root)
    monkeypatch.setattr(probe, "run_command", lambda *args: raw)
    status = policy.reconcile_display(WaylandSession())
    assert status.reason == "malformed_output" and "SECRET" not in str(
        status.public_diagnostics()
    )


def test_disabled_output_omits_layout_fields(display):
    root, calls, states = display
    connector(root)
    disabled = dict(name="HDMI-A-1", enabled=False, modes=[mode(current=False)])
    states[:] = [[disabled], [], [output()]]
    assert policy.reconcile_display(WaylandSession()).state == "ready"
    assert "--on" in calls[1]


def test_monitor_wait_is_interruptible_and_pre_cancel_does_no_work(display):
    root, calls, _ = display
    connector(root)
    stop = Event()
    monitor = policy.monitor_display(WaylandSession(), stop)
    assert next(monitor).state == "ready"
    thread = Thread(target=lambda: list(monitor))
    thread.start()
    started = time.monotonic()
    stop.set()
    thread.join(0.5)
    assert not thread.is_alive() and time.monotonic() - started < 0.5
    assert len(calls) == 1
    assert list(policy.monitor_display(WaylandSession(), stop)) == []


def test_failed_monitor_waits_before_retry(display, monkeypatch):
    root, calls, _ = display
    connector(root)
    monkeypatch.setattr(probe, "run_command", lambda *args: b"bad")
    stop = Event()
    snapshots = []
    thread = Thread(
        target=lambda: snapshots.extend(policy.monitor_display(WaylandSession(), stop))
    )
    thread.start()
    time.sleep(0.1)
    stop.set()
    thread.join(0.5)
    assert not thread.is_alive()
    assert [status.reason for status in snapshots] == ["malformed_output"]
