"""Software contracts only: fake host tools do not establish physical support."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from postcardscene.graphics.output import DisplayStatus
from postcardscene.power import (
    CecBackend,
    DdcBackend,
    Reason,
    SignalBackend,
    State,
    Status,
)
from postcardscene.power._types import PowerError

CEC_INFO = b"""Driver Info:
    Capabilities               : 0x0000010e
        Transmit
    Physical Address           : 1.0.0.0
    Logical Address Mask       : 0x0010
      Logical Address          : 4 (Playback Device 1)
"""
DDC_ONE = b"Display 1\n   I2C bus: /dev/i2c-7\n   Monitor: MFG:MODEL:PRIVATE-SERIAL\n"
DDC_ONE = DDC_ONE.replace(
    b"   Monitor:",
    b"   DRM connector: card1-HDMI-A-1\n   drm_connector_id: 42\n   Monitor:",
)
DDC_TWO = (
    DDC_ONE + b"\nDisplay 2\n   I2C bus: /dev/i2c-8\n   Monitor: OTHER:MODEL:PRIVATE\n"
)


def cec_reply(value):
    return (
        "Transmit from Playback Device 1 to TV (4 to 0):\n"
        "GIVE_DEVICE_POWER_STATUS (0x8f)\n"
        "\tRaw: 0x40 0x8f (@ )\n"
        "    Received from TV (0):\n    REPORT_POWER_STATUS (0x90):\n"
        f"\tpower-status: on (0x{value:02x})\n"
        f"\tRaw: 0x04 0x90 0x{value:02x} (   )\n"
        "\tSequence: 1 Tx Timestamp: 1.000s Rx Timestamp: 1.001s\n"
    ).encode()


def scripted(monkeypatch, backend, replies):
    calls = []
    replies = iter(replies)

    def run(arguments, environment, cancelled):
        calls.append((list(arguments), dict(environment)))
        if cancelled():
            raise PowerError(Reason.CANCELLED)
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(backend._command, "run", run)
    return calls


@pytest.mark.parametrize(
    "selector",
    ["cec0", "/dev/cec01", "/dev/cec10000", "/dev/cec0;id", "/dev/../cec0", 1],
)
def test_cec_rejects_untrusted_selector(selector):
    with pytest.raises(ValueError, match="trusted CEC"):
        CecBackend(selector)


def test_cec_unconfigured_and_cancelled_do_not_launch(monkeypatch):
    backend = CecBackend()
    calls = scripted(monkeypatch, backend, [])
    assert backend.probe(lambda: False).status == Status.UNAVAILABLE
    assert backend.request_on(lambda: True).status == Status.CANCELLED
    assert not calls


@pytest.mark.parametrize(
    "requested,message,value",
    [(State.ON, "--image-view-on", 0), (State.OFF, "--standby", 1)],
)
def test_cec_tv_only_request_then_authoritative_status(
    monkeypatch, requested, message, value
):
    backend = CecBackend("/dev/cec0")
    calls = scripted(monkeypatch, backend, [CEC_INFO, b"", cec_reply(value)])
    result = (backend.request_on if requested == State.ON else backend.request_off)(
        lambda: False
    )
    assert result.status == Status.PHYSICAL
    assert result.physical == result.requested == requested
    assert result.signal == State.UNKNOWN
    assert calls[0] == (["--device", "/dev/cec0"], {})
    assert [call[0][-1] for call in calls[1:]] == [
        message,
        "--give-device-power-status",
    ]
    assert calls[1][0][2:-1] == [
        "--skip-info",
        "--show-raw",
        "--from",
        "4",
        "--to",
        "0",
        "--timeout",
        "1000",
    ]
    with pytest.raises(FrozenInstanceError):
        result.physical = State.OFF


@pytest.mark.parametrize(
    "raw",
    [
        cec_reply(2),
        cec_reply(3),
        cec_reply(255),
        b"",
        b"Transmit failed",
        cec_reply(0).replace(b"TV (0)", b"Audio System (5)"),
        cec_reply(0).replace(b"0x04 0x90", b"0x05 0x90"),
        cec_reply(0) * 2,
    ],
)
def test_cec_missing_malformed_transitional_reply_is_unknown(monkeypatch, raw):
    backend = CecBackend("/dev/cec1")
    scripted(monkeypatch, backend, [CEC_INFO, raw])
    assert backend.observe(lambda: False).physical == State.UNKNOWN


def test_cec_lost_adapter_authority_does_not_send(monkeypatch):
    backend = CecBackend("/dev/cec0")
    calls = scripted(
        monkeypatch, backend, [CEC_INFO.replace(b"0x0000010e", b"0x00000106")]
    )
    assert backend.request_off(lambda: False).status == Status.DEGRADED
    assert len(calls) == 1


@pytest.mark.parametrize("selector", [True, 0, -1, 10000, "1", "1;id"])
def test_ddc_selector_is_bounded_positive_integer(selector):
    with pytest.raises(ValueError, match="trusted DDC"):
        DdcBackend(selector)


def test_ddc_ambiguous_discovery_never_writes(monkeypatch):
    backend = DdcBackend()
    calls = scripted(monkeypatch, backend, [DDC_TWO])
    assert backend.request_off(lambda: False).reason == Reason.AMBIGUOUS
    assert len(calls) == 1


def test_ddc_explicit_display_and_physical_readback(monkeypatch):
    backend = DdcBackend(2)
    calls = scripted(
        monkeypatch, backend, [DDC_TWO, b"VCP D6 SNC x01\n", b"", b"VCP D6 SNC x04\n"]
    )
    result = backend.request_off(lambda: False)
    assert result.status == Status.PHYSICAL
    assert result.physical == State.OFF
    assert all(call[0][:2] == ["--noconfig", "--brief"] for call in calls)
    assert calls[2][0][2:] == ["--bus", "8", "--noverify", "setvcp", "D6", "0x04"]
    assert calls[3][0][2:] == ["--bus", "8", "getvcp", "D6"]
    assert "PRIVATE" not in repr(result)


def test_ddc_post_standby_query_loss_retains_bus_for_later_wake(monkeypatch):
    backend = DdcBackend()
    calls = scripted(
        monkeypatch,
        backend,
        [
            DDC_ONE,
            b"VCP D6 SNC x01\n",
            b"",
            PowerError(Reason.TOOL_FAILED),
            b"",
            b"VCP D6 SNC x01\n",
        ],
    )
    off = backend.request_off(lambda: False)
    assert off.physical == State.UNKNOWN and off.status == Status.DEGRADED
    assert off.requested == State.OFF
    on = backend.request_on(lambda: False)
    assert on.physical == State.ON
    assert calls[4][0][2:] == ["--bus", "7", "--noverify", "setvcp", "D6", "0x01"]
    assert sum("detect" in args for args, _ in calls) == 1


@pytest.mark.parametrize(
    "value,state",
    [
        (1, State.ON),
        (2, State.OFF),
        (3, State.OFF),
        (4, State.OFF),
        (5, State.UNKNOWN),
        (255, State.UNKNOWN),
    ],
)
def test_ddc_standard_power_values(monkeypatch, value, state):
    backend = DdcBackend()
    scripted(
        monkeypatch,
        backend,
        [DDC_ONE, b"VCP D6 SNC x01\n", f"VCP D6 SNC x{value:02x}\n".encode()],
    )
    assert backend.observe(lambda: False).physical == state


@pytest.mark.parametrize(
    "raw",
    [
        b"VCP D6 ERR\n",
        b"VCP D6 SNC x01\nVCP D6 SNC x04\n",
        b"secret error",
        b"VCP D6 C 1 5\n",
    ],
)
def test_ddc_bad_capability_never_writes(monkeypatch, raw):
    backend = DdcBackend()
    calls = scripted(monkeypatch, backend, [DDC_ONE, raw])
    assert backend.request_off(lambda: False).physical == State.UNKNOWN
    assert len(calls) == 2


@pytest.mark.parametrize(
    "raw",
    [b"secret error", DDC_ONE * 2, DDC_ONE + b"Invalid display\n", b"Display 1\n"],
)
def test_ddc_malformed_discovery_is_sanitized(monkeypatch, raw):
    backend = DdcBackend()
    scripted(monkeypatch, backend, [raw])
    result = backend.probe(lambda: False)
    assert result.status in (Status.DEGRADED, Status.UNAVAILABLE)
    assert "secret" not in repr(result)


def signal_backend(state="ready", connector="HDMI-A-1"):
    session = SimpleNamespace(
        inspect=lambda: SimpleNamespace(available=True),
        client_environment=lambda: {
            "WAYLAND_DISPLAY": "wayland-0",
            "XDG_RUNTIME_DIR": "/run/private",
        },
    )
    return SignalBackend(session, DisplayStatus(True, state, state, connector))


def signal_reply(mode):
    value = 1 if mode == "on" else 0
    return (
        '[1.000] wl_output@5.name("HDMI-A-1")\n'
        "[1.001]  -> zwlr_output_power_manager_v1@6.get_output_power(new id zwlr_output_power_v1@7, wl_output@5)\n"
        f"[1.002] zwlr_output_power_v1@7.mode({value})\n"
        f"HDMI-A-2 on\nHDMI-A-1 {mode}\n"
    ).encode()


@pytest.mark.parametrize("requested,mode", [(State.ON, "on"), (State.OFF, "off")])
def test_signal_only_selected_output_and_session_environment(
    monkeypatch, requested, mode
):
    backend = signal_backend()
    calls = scripted(monkeypatch, backend, [b"", signal_reply(mode)])
    result = (backend.request_on if requested == State.ON else backend.request_off)(
        lambda: False
    )
    assert result.status == Status.SIGNAL_ONLY and result.signal == requested
    assert result.physical == State.UNKNOWN
    assert calls[0][0] == ["--" + mode, "HDMI-A-1"]
    assert calls[1][0] == []
    assert calls[0][1] == calls[1][1] == backend._session.client_environment()


@pytest.mark.parametrize(
    "raw,reason",
    [
        (signal_reply("on"), Reason.MISMATCH),
        (b"HDMI-A-2 off\n", Reason.UNAVAILABLE),
        (b"HDMI-A-1 off\nHDMI-A-1 on\n", Reason.MALFORMED),
        (b"HDMI-A-1 unknown\n", Reason.MALFORMED),
    ],
)
def test_signal_no_false_success(monkeypatch, raw, reason):
    backend = signal_backend()
    scripted(monkeypatch, backend, [b"", raw])
    result = backend.request_off(lambda: False)
    assert result.physical == State.UNKNOWN and result.reason == reason


def test_signal_requires_ready_selection(monkeypatch):
    backend = signal_backend(state="ambiguous")
    calls = scripted(monkeypatch, backend, [])
    assert backend.request_on(lambda: False).status == Status.UNAVAILABLE
    assert not calls


def test_cleanup_failure_is_distinct_and_latches_no_further_commands(monkeypatch):
    backend = CecBackend("/dev/cec0")
    backend._command.cleanup_failed = True
    calls = scripted(monkeypatch, backend, [])
    for method in (
        backend.probe,
        backend.observe,
        backend.request_on,
        backend.request_off,
    ):
        result = method(lambda: False)
        assert result.status == Status.CLEANUP_FAILED and result.cleanup_failed
    assert not calls


@pytest.mark.parametrize(
    "raw",
    [
        b"HDMI-A-1 off\n",
        signal_reply("off").replace(b".mode(0)", b".failed()"),
        signal_reply("off").replace(b".mode(0)", b".mode(1)"),
        signal_reply("off").replace(b"wl_output@5.name", b"wl_output@9.name"),
        signal_reply("off") + b"[2.000] wl_registry@2.global_remove(4)\n",
    ],
)
def test_signal_requires_matching_protocol_mode_not_default_off(monkeypatch, raw):
    backend = signal_backend()
    scripted(monkeypatch, backend, [raw])
    result = backend.observe(lambda: False)
    assert result.signal == result.physical == State.UNKNOWN
    assert result.evidence == "none"


def test_ddc_ignores_complete_unresponsive_display_entry(monkeypatch):
    backend = DdcBackend()
    invalid = (
        b"Invalid display\n   I2C bus: /dev/i2c-9\n   Monitor: INTERNAL:LCD:PRIVATE\n"
    )
    scripted(
        monkeypatch,
        backend,
        [DDC_ONE + invalid, b"VCP D6 SNC x01\n", b"VCP D6 SNC x01\n"],
    )
    assert backend.probe(lambda: False).status == Status.PHYSICAL
