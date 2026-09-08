"""Common four-operation boundary for the three concrete panel capabilities."""

from ._command import Cancelled, Command
from ._types import Kind, PowerError, PowerResult, Reason, State, Status


class Backend:
    """Calls must be serialized by the later coordinator; no retries or workers."""

    def __init__(self, kind: Kind):
        self.kind = kind
        self._command = Command(kind)

    def probe(self, cancelled: Cancelled) -> PowerResult:
        return self._perform(None, cancelled)

    def observe(self, cancelled: Cancelled) -> PowerResult:
        return self._perform(None, cancelled)

    def request_on(self, cancelled: Cancelled) -> PowerResult:
        return self._perform(State.ON, cancelled)

    def request_off(self, cancelled: Cancelled) -> PowerResult:
        return self._perform(State.OFF, cancelled)

    def _perform(self, requested, cancelled):
        try:
            if self._command.cleanup_failed:
                raise PowerError(Reason.CLEANUP_FAILED, cleanup_failed=True)
            if cancelled():
                raise PowerError(Reason.CANCELLED)
            state = self._operate(requested, cancelled)
        except PowerError as error:
            status = Status.DEGRADED
            if error.reason in (
                Reason.NOT_CONFIGURED,
                Reason.UNAVAILABLE,
                Reason.TOOL_MISSING,
                Reason.AMBIGUOUS,
            ):
                status = Status.UNAVAILABLE
            # A request may have completed before its readback lost authority.
            # Its failure is never a capability-unavailable fallback invitation.
            if requested is not None and error.reason != Reason.NOT_CONFIGURED:
                status = Status.DEGRADED
            if error.reason == Reason.CANCELLED:
                status = Status.CANCELLED
            if error.cleanup_failed:
                status = Status.CLEANUP_FAILED
            return PowerResult(
                self.kind,
                status,
                error.reason,
                requested,
                cleanup_failed=error.cleanup_failed,
            )
        signal_only = self.kind == Kind.SIGNAL
        reason = Reason.SIGNAL_ONLY if signal_only else Reason.CONFIRMED
        status = Status.SIGNAL_ONLY if signal_only else Status.PHYSICAL
        if state == State.UNKNOWN:
            status, reason = Status.DEGRADED, Reason.UNKNOWN
        elif requested is not None and state != requested:
            status, reason = Status.DEGRADED, Reason.MISMATCH
        return PowerResult(
            self.kind,
            status,
            reason,
            requested,
            physical=State.UNKNOWN if signal_only else state,
            signal=state if signal_only else State.UNKNOWN,
        )

    def _operate(self, requested: State | None, cancelled: Cancelled) -> State:
        raise NotImplementedError
