"""Bounded panel-only client; no runtime construction or hardware dependencies."""

import json
import socket
from dataclasses import dataclass

SOCKET_PATH = "/run/postcardscene/panel-control.sock"
LIMIT = 1024
TIMEOUT = 0.1

# Closed wire vocabulary: never pass arbitrary runtime strings to the control UI.
VOCABULARY = {
    "state": ("starting", "ready", "degraded", "stopping", "stopped", "error"),
    "configured_backend": ("auto", "cec", "ddc", "signal"),
    "active_backend": (None, "cec", "ddc", "signal"),
    "physical": ("on", "off", "unknown"),
    "signal": ("on", "off", "unknown"),
    "evidence": ("physical", "signal_only", "none"),
    "reason": (
        "confirmed",
        "signal_only",
        "not_configured",
        "unavailable",
        "ambiguous",
        "malformed_output",
        "unknown_state",
        "state_mismatch",
        "tool_missing",
        "tool_failed",
        "tool_timeout",
        "oversized_output",
        "cancelled",
        "cleanup_failed",
        "settings_unavailable",
        "wake_delay",
    ),
}
BOOL_FIELDS = (
    "desired_active",
    "operating_active",
    "protection_sleep",
    "signal_sleep_owned",
    "intentional_signal_sleep",
)


@dataclass(frozen=True)
class PanelResponse:
    outcome: str = "unavailable"
    status: dict | None = None


def _decode(raw):
    response = json.loads(raw)
    if (
        type(response) is not dict
        or set(response) != {"version", "outcome", "status"}
        or type(response["version"]) is not int
        or response["version"] != 1
        or response["outcome"]
        not in ("accepted", "unavailable", "rejected", "cleanup_failed")
    ):
        raise ValueError
    status = response["status"]
    if type(status) is not dict or type(status.get("configured")) is not bool:
        raise ValueError
    if not status["configured"]:
        if status != {
            "configured": False,
            "state": "unavailable",
            "reason": "not_configured",
        }:
            raise ValueError
        return PanelResponse(response["outcome"])
    if set(status) != set(VOCABULARY) | set(BOOL_FIELDS) | {
        "configured",
        "diagnostic_active",
    }:
        raise ValueError
    for key, allowed in VOCABULARY.items():
        if status[key] not in allowed:
            raise ValueError
    if any(type(status[key]) is not bool for key in BOOL_FIELDS):
        raise ValueError
    if (
        status["diagnostic_active"] is not None
        and type(status["diagnostic_active"]) is not bool
    ):
        raise ValueError
    if status["evidence"] != "physical" and status["physical"] != "unknown":
        raise ValueError
    return PanelResponse(response["outcome"], status)


def _exchange(action):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
            client.settimeout(TIMEOUT)
            client.connect(SOCKET_PATH)
            client.sendall(json.dumps({"version": 1, "action": action}).encode("ascii"))
            raw, _, flags, _ = client.recvmsg(LIMIT)
            if not raw or flags & socket.MSG_TRUNC:
                return PanelResponse()
        return _decode(raw)
    except (OSError, ValueError, TypeError, RecursionError):
        return PanelResponse()


def read_status():
    return _exchange("status")


def test_wake():
    return _exchange("test_on")


def test_sleep():
    return _exchange("test_off")
