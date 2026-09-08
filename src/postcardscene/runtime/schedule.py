"""Current-state schedule convergence; real operating target wiring belongs to #104."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock, Thread
from time import monotonic
from typing import Literal, Protocol

from postcardscene.operating_schedule import (
    clear_expired_override,
    evaluate_operating_schedule,
    get_operating_schedule,
)

POLL_SECONDS = 15.0
JOIN_SECONDS = 5.0


class OperatingResult(StrEnum):
    APPLIED = "applied"
    UNAVAILABLE = "unavailable"
    CLEANUP_FAILED = "cleanup_failed"


class OperatingTarget(Protocol):
    """Bounded synchronous operation; observe cancellation and return fixed results.

    Ordinary exceptions mean unavailable. Uncertain ownership/cleanup MUST return
    CLEANUP_FAILED. The coordinator cannot interrupt an uncooperative callback.
    """

    def __call__(
        self, active: bool, cancelled: Callable[[], bool]
    ) -> OperatingResult: ...


@dataclass(frozen=True)
class ScheduleStatus:
    state: Literal["starting", "ready", "degraded", "stopping", "stopped", "error"] = (
        "starting"
    )
    desired_active: bool | None = None
    applied_active: bool | None = None
    reason: Literal[
        "schedule", "override", "schedule_disabled", "unavailable", "cleanup_failed"
    ] = "unavailable"


class ScheduleWorker:
    """Start once with RuntimeHost.stop_event; join cancels within five seconds."""

    def __init__(
        self,
        database,
        target: OperatingTarget,
        stop_event,
        *,
        clock=lambda: datetime.now(UTC),
        wait=None,
        elapsed=monotonic,
    ):
        self.database = database
        self.target = target
        self.stop_event = stop_event
        self._clock = clock
        self._wait = wait or stop_event.wait
        self._elapsed = elapsed
        self._lock = Lock()
        self._status = ScheduleStatus()
        self._failure = None
        # Preserve process exit if a target violates its bounded-call contract.
        self.thread = Thread(target=self._run, name="schedule", daemon=True)

    @property
    def status(self):
        with self._lock:
            return self._status

    @property
    def failure(self):
        with self._lock:
            return self._failure

    def _publish(self, **changes):
        with self._lock:
            if self._failure is None:
                self._status = replace(self._status, **changes)

    def _fatal(self, reason):
        with self._lock:
            if self._failure is None:
                self._failure = reason
            self._status = replace(self._status, state="error", reason="cleanup_failed")
        self.stop_event.set()

    def start(self):
        self.thread.start()

    def request_shutdown(self):
        with self._lock:
            if self._status.state not in {"stopped", "error"}:
                self._status = replace(self._status, state="stopping")
        self.stop_event.set()

    def join(self):
        self.request_shutdown()
        self.thread.join(JOIN_SECONDS)
        if self.thread.is_alive():
            self._fatal("shutdown_timeout")
        if self.failure is not None:
            raise RuntimeError("Schedule worker failed: " + self.failure)

    def _tick(self):
        try:
            snapshot = get_operating_schedule(self.database)
            now = self._clock()
            decision = evaluate_operating_schedule(snapshot, now)
        except Exception:
            self._publish(state="degraded", reason="unavailable")
            return
        self._publish(desired_active=decision.active)
        cleanup_unavailable = False
        if decision.override_expired and not self.stop_event.is_set():
            try:
                clear_expired_override(self.database, snapshot, now=now)
            except Exception:
                cleanup_unavailable = True
        if self.stop_event.is_set():
            return
        if self.status.applied_active != decision.active:
            try:
                result = self.target(decision.active, self.stop_event.is_set)
            except Exception:
                result = OperatingResult.UNAVAILABLE
            if result == OperatingResult.CLEANUP_FAILED:
                self._fatal("cleanup_failed")
                return
            if result != OperatingResult.APPLIED:
                self._publish(state="degraded", reason="unavailable")
                return
            self._publish(applied_active=decision.active)
        self._publish(
            state="degraded" if cleanup_unavailable else "ready",
            reason="unavailable" if cleanup_unavailable else decision.reason,
        )

    def _run(self):
        try:
            while not self.stop_event.is_set():
                started = self._elapsed()
                self._tick()
                # Account for bounded operation time; never replay missed ticks.
                if not self.stop_event.is_set():
                    self._wait(max(0.0, POLL_SECONDS - (self._elapsed() - started)))
        except BaseException:
            self._fatal("worker_failed")
        finally:
            self._publish(state="stopped")
