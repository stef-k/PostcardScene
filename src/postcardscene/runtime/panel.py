"""Single, independently constructed panel owner; no playback or host wiring."""

from concurrent.futures import Future
from dataclasses import dataclass, replace
from enum import StrEnum
from threading import Event, Lock, Thread
from time import monotonic
from typing import Literal

from postcardscene.power import Kind, PowerResult, Reason, State, Status
from postcardscene.settings import get_display_power_settings

POLL_SECONDS = 15.0
TEST_SECONDS = 5.0
JOIN_SECONDS = 5.0


class Outcome(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    STOPPED = "stopped"
    CLEANUP_FAILED = "cleanup_failed"


@dataclass(frozen=True)
class PanelStatus:
    state: Literal["starting", "ready", "degraded", "stopping", "stopped", "error"] = (
        "starting"
    )
    configured_backend: Literal["auto", "cec", "ddc", "signal"] = "auto"
    active_backend: Kind | None = None
    desired_active: bool = True
    operating_active: bool = True
    protection_sleep: bool = False
    diagnostic_active: bool | None = None
    physical: State = State.UNKNOWN
    signal: State = State.UNKNOWN
    evidence: Literal["physical", "signal_only", "none"] = "none"
    reason: Reason | Literal["settings_unavailable", "wake_delay"] = Reason.UNAVAILABLE


class PanelCoordinator:
    """Own injected #111 capabilities exclusively. Settings reads must be bounded.

    Intent methods return one generation's Future. Callers use result(timeout=...)
    to wait boundedly; timeout does not retract policy. A newer intent supersedes
    the prior pending generation, without a replay queue. Only READY establishes
    effective target convergence (inspect status for protection and evidence).
    """

    def __init__(self, database, stop_event, *, cec, ddc, signal, clock=monotonic):
        self.database = database
        self.stop_event = stop_event
        self._backends = (cec, ddc, signal)
        self._clock = clock
        self._lock = Lock()
        self._changed = Event()
        self._status = PanelStatus()
        self._failure = None
        self._generation = 0
        self._pending = None
        self._expires = None
        self._selected = None
        self._usable = False
        self._command_issued = False
        self._selection_choice = None
        self._wake_ready_at = None
        self._wake_pending = False
        self._pass_generation = 0
        self._deadline = float("inf")
        self.thread = Thread(target=self._run, name="panel-power", daemon=True)

    @property
    def status(self):
        with self._lock:
            return self._status

    @property
    def failure(self):
        with self._lock:
            return self._failure

    def _publish(self, *, generation=None, **changes):
        with self._lock:
            if self._failure is None and (
                generation is None or generation == self._generation
            ):
                self._status = replace(self._status, **changes)

    def _intent(self, field, value):
        if type(value) is not bool:
            raise ValueError("Panel intent must be boolean")
        result = Future()
        result.set_running_or_notify_cancel()
        with self._lock:
            previous = self._pending
            if self.stop_event.is_set():
                outcome = Outcome.STOPPED
            elif (
                field == "diagnostic_active" and value and self._status.protection_sleep
            ):
                outcome = Outcome.REJECTED
            else:
                self._status = replace(self._status, **{field: value}, state="starting")
                if field == "diagnostic_active":
                    self._expires = self._clock() + TEST_SECONDS
                self._generation += 1
                self._pending = result
                outcome = None
        # Future callbacks may reenter the coordinator; never run them under lock.
        if outcome is not None:
            result.set_result(outcome)
        else:
            if previous is not None:
                previous.set_result(Outcome.SUPERSEDED)
            self._changed.set()
        return result

    def apply_operating(self, active: bool) -> Future[Outcome]:
        return self._intent("operating_active", active)

    def set_protection(self, sleep: bool) -> Future[Outcome]:
        return self._intent("protection_sleep", sleep)

    def request_test(self, active: bool) -> Future[Outcome]:
        return self._intent("diagnostic_active", active)

    def start(self):
        self.thread.start()

    def join(self):
        with self._lock:
            if self._status.state not in {"stopped", "error"}:
                self._status = replace(self._status, state="stopping")
        self.stop_event.set()
        self._changed.set()
        self.thread.join(JOIN_SECONDS)
        if self.thread.is_alive():
            self._fatal("shutdown_timeout")
        if self.failure is not None:
            raise RuntimeError("Panel coordinator failed: " + self.failure)

    def _fatal(self, reason):
        with self._lock:
            self._failure = self._failure or reason
            self._status = replace(self._status, state="error", reason="cleanup_failed")
        self.stop_event.set()
        self._changed.set()

    def _settle(self, generation, outcome):
        with self._lock:
            pending = None
            if generation == self._generation:
                pending, self._pending = self._pending, None
        if pending is not None:
            pending.set_result(outcome)

    def _expire_test(self):
        expired = None
        with self._lock:
            if self._expires is not None and self._clock() >= self._expires:
                self._expires = None
                self._status = replace(self._status, diagnostic_active=None)
                expired, self._pending = self._pending, None
                self._generation += 1
        if expired is not None:
            expired.set_result(Outcome.SUPERSEDED)

    def _target(self):
        with self._lock:
            s = self._status
            active = not s.protection_sleep and (
                s.operating_active
                if s.diagnostic_active is None
                else s.diagnostic_active
            )
            self._status = replace(s, desired_active=active)
            return self._generation, active

    def _cancelled(self):
        with self._lock:
            return (
                self.stop_event.is_set()
                or self._generation != self._pass_generation
                or self._clock() >= self._deadline
                or (self._expires is not None and self._clock() >= self._expires)
            )

    def _call(self, backend, method):
        if self._cancelled():
            return PowerResult(backend.kind, Status.CANCELLED, Reason.CANCELLED)
        result = getattr(backend, method)(self._cancelled)
        if result.cleanup_failed or result.status == Status.CLEANUP_FAILED:
            self._fatal("cleanup_failed")
        return result

    @staticmethod
    def _matches(result, active):
        wanted = State.ON if active else State.OFF
        return (result.status == Status.PHYSICAL and result.physical == wanted) or (
            result.status == Status.SIGNAL_ONLY and result.signal == wanted
        )

    def _select(self, choice):
        candidates = self._backends
        if choice != "auto":
            candidates = (self._backends[("cec", "ddc", "signal").index(choice)],)
        for backend in candidates:
            result = self._call(backend, "probe")
            if self._cancelled():
                return result
            # Unknown capability is not permission to probe another owner.
            if result.status != Status.UNAVAILABLE or choice != "auto":
                self._selected = backend
                self._command_issued = False
                self._usable = result.status in {Status.PHYSICAL, Status.SIGNAL_ONLY}
                self._selection_choice = choice
                return result
        return result

    def _reconcile(self, active, settings):
        choice = settings.display_power_backend
        if self._selected is None:
            result = self._select(choice)
        else:
            result = self._call(self._selected, "observe")
            # Never abandon sleeping or uncertain authority, even on settings edits.
            if (
                choice != self._selection_choice
                and not self._wake_pending
                and (
                    not self._command_issued or (active and self._matches(result, True))
                )
            ):
                self._selected = None
                result = self._select(choice)
        if self._cancelled():
            return False
        if result.status in {Status.PHYSICAL, Status.SIGNAL_ONLY}:
            self._usable = True
        if self._selected is not None and not self._matches(result, active):
            if self._usable and result.status != Status.CANCELLED:
                self._command_issued = True
                self._wake_pending = active
                self._wake_ready_at = None
                result = self._call(
                    self._selected, "request_on" if active else "request_off"
                )
        if self._cancelled():
            return False
        self._publish(
            generation=self._pass_generation,
            active_backend=self._selected.kind if self._selected else None,
            physical=result.physical,
            signal=result.signal,
            evidence=result.evidence,
        )
        return self._readiness(result, active, settings.display_wake_delay_seconds)

    def _readiness(self, result, active, wake_delay):
        matched = self._matches(result, active)
        if not active:
            self._wake_pending = False
            self._wake_ready_at = None
        elif not matched:
            self._wake_ready_at = None
        elif self._wake_pending and self._wake_ready_at is None:
            self._wake_ready_at = self._clock() + wake_delay
        if matched and active and self._wake_ready_at is not None:
            if self._clock() < self._wake_ready_at:
                self._publish(
                    generation=self._pass_generation,
                    state="starting",
                    reason="wake_delay",
                )
                return None
            self._wake_ready_at = None
            self._wake_pending = False
        self._publish(
            generation=self._pass_generation,
            state="ready" if matched else "degraded",
            reason=result.reason,
        )
        return matched

    def _tick(self):
        self._deadline = self._clock() + POLL_SECONDS
        self._expire_test()
        try:
            settings = get_display_power_settings(self.database)
        except Exception:
            self._publish(state="degraded", reason="settings_unavailable")
            self._settle(self._generation, Outcome.DEGRADED)
            return
        self._publish(configured_backend=settings.display_power_backend)
        generation, active = self._target()
        self._pass_generation = generation
        ready = self._reconcile(active, settings)
        if self._cancelled():
            if self._clock() >= self._deadline:
                self._publish(
                    generation=generation,
                    state="degraded",
                    reason=Reason.TIMEOUT,
                    physical=State.UNKNOWN,
                    signal=State.UNKNOWN,
                    evidence="none",
                )
                self._settle(generation, Outcome.DEGRADED)
            return
        if ready is not None:
            self._settle(generation, Outcome.READY if ready else Outcome.DEGRADED)

    def _wait(self, deadline):
        # Event.wait supplies immediate intent wakeups; short slices also observe
        # the independently owned shared stop event without a helper thread.
        while not self.stop_event.is_set():
            remaining = deadline - self._clock()
            if remaining <= 0 or self._changed.wait(min(remaining, 0.05)):
                return

    def _run(self):
        try:
            while not self.stop_event.is_set():
                self._changed.clear()
                started = self._clock()
                self._tick()
                with self._lock:
                    deadlines = [started + POLL_SECONDS]
                    if self._expires is not None:
                        deadlines.append(self._expires)
                    if (
                        self._wake_ready_at is not None
                        and self._wake_ready_at > self._clock()
                    ):
                        deadlines.append(self._wake_ready_at)
                self._wait(min(deadlines))
        except BaseException:
            self._fatal("worker_failed")
        finally:
            self._publish(state="stopped", diagnostic_active=None)
            self._settle(
                self._generation,
                Outcome.CLEANUP_FAILED if self.failure else Outcome.STOPPED,
            )
