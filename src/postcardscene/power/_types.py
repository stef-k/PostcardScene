"""Closed panel capability results; no device or tool data is public."""

from dataclasses import dataclass
from enum import StrEnum


class State(StrEnum):
    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


class Reason(StrEnum):
    CONFIRMED = "confirmed"
    SIGNAL_ONLY = "signal_only"
    NOT_CONFIGURED = "not_configured"
    UNAVAILABLE = "unavailable"
    AMBIGUOUS = "ambiguous"
    MALFORMED = "malformed_output"
    UNKNOWN = "unknown_state"
    MISMATCH = "state_mismatch"
    TOOL_MISSING = "tool_missing"
    TOOL_FAILED = "tool_failed"
    TIMEOUT = "tool_timeout"
    OVERSIZED = "oversized_output"
    CANCELLED = "cancelled"
    CLEANUP_FAILED = "cleanup_failed"


class Kind(StrEnum):
    CEC = "cec"
    DDC = "ddc"
    SIGNAL = "signal"


class Status(StrEnum):
    PHYSICAL = "physical"
    SIGNAL_ONLY = "signal_only"
    UNAVAILABLE = "unavailable"
    DEGRADED = "degraded"
    CANCELLED = "cancelled"
    CLEANUP_FAILED = "cleanup_failed"


@dataclass(frozen=True)
class PowerResult:
    backend: Kind
    status: Status
    reason: Reason
    requested: State | None = None
    physical: State = State.UNKNOWN
    signal: State = State.UNKNOWN
    cleanup_failed: bool = False

    @property
    def evidence(self) -> str:
        if self.physical != State.UNKNOWN:
            return "physical"
        if self.signal != State.UNKNOWN:
            return "signal_only"
        return "none"


class PowerError(Exception):
    def __init__(self, reason: Reason, *, cleanup_failed: bool = False):
        super().__init__(reason.value)
        self.reason = reason
        self.cleanup_failed = cleanup_failed
