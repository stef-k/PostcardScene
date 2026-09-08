"""One runtime consumer of the graphics monitor, serialized with signal power."""

from dataclasses import dataclass, replace
from threading import Thread

from postcardscene.graphics.output import (
    DisplayStatus,
    monitor_display,
    reconcile_display,
)


@dataclass(frozen=True)
class OutputMonitorStatus:
    state: str = "stopped"
    reason: str = "stopped"
    display: DisplayStatus | None = None


class OutputMonitor:
    def __init__(
        self,
        session,
        stop_event,
        mutation_guard,
        *,
        connector_override=None,
        panel_coordinator=None,
    ):
        self.session = session
        self.stop_event = stop_event
        self.mutation_guard = mutation_guard
        self.connector_override = connector_override
        self.panel_coordinator = panel_coordinator
        self.status = OutputMonitorStatus()
        self.failure = None
        self.thread = Thread(target=self.run, name="output-monitor", daemon=True)

    def start(self):
        self.thread.start()

    def join(self):
        self.thread.join(5.0)
        if self.thread.is_alive():
            self.stop_event.set()
            self.status = replace(self.status, state="error", reason="cleanup_failed")
            raise RuntimeError("Output monitor did not stop within the cleanup bound.")
        if self.failure is not None:
            raise RuntimeError("Output monitor failed.") from self.failure

    def reconcile(self):
        if not self.mutation_guard.acquire(self.stop_event.is_set):
            return None
        try:
            # Intent may change while waiting for signal's command/readback.
            if (
                self.panel_coordinator is not None
                and self.panel_coordinator.intentional_signal_sleep
            ):
                return None
            if self.stop_event.is_set():
                return None
            return reconcile_display(
                self.session,
                connector_override=self.connector_override,
                stop_event=self.stop_event,
            )
        except BaseException:
            # Publish fatal cancellation before the other owner can acquire.
            self.stop_event.set()
            raise
        finally:
            self.mutation_guard.release()

    def run(self):
        try:
            for display in monitor_display(
                self.session,
                self.stop_event,
                connector_override=self.connector_override,
                reconcile=self.reconcile,
            ):
                # Publication and the monitor's inter-pass wait are outside the guard.
                self.status = (
                    replace(
                        self.status,
                        state="suspended",
                        reason="intentional_signal_sleep",
                    )
                    if display is None
                    else OutputMonitorStatus("running", "running", display)
                )
            self.status = replace(self.status, state="stopped", reason="stopped")
        except BaseException as error:
            self.failure = error
            self.status = replace(self.status, state="error", reason="worker_failed")
            self.stop_event.set()
