"""Transport-independent runtime lifecycle; no web or hardware initialization."""

from dataclasses import dataclass
from enum import StrEnum
from threading import Event


class Lifecycle(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


_SUMMARIES = {
    Lifecycle.STARTING: "Runtime is starting.",
    Lifecycle.RUNNING: "Runtime is running.",
    Lifecycle.DEGRADED: "Runtime is running with an optional responsibility impaired.",
    Lifecycle.STOPPING: "Runtime is stopping.",
    Lifecycle.STOPPED: "Runtime has stopped.",
    Lifecycle.ERROR: "Runtime failed and cannot continue.",
}


@dataclass(frozen=True)
class RuntimeStatus:
    state: Lifecycle
    summary: str


class RuntimeHost:
    """One host per process, run once on the owning thread.

    Control callers may read status and request shutdown. Lifecycle changes and
    optional-failure reporting belong to the owning thread. Future components
    observe stop_event (never clear it) and must bound work and cleanup.
    Panel power is separate from this lifecycle.
    """

    def __init__(self):
        self.stop_event = Event()
        self._status = RuntimeStatus(Lifecycle.STARTING, _SUMMARIES[Lifecycle.STARTING])
        self._has_run = False

    @property
    def status(self) -> RuntimeStatus:
        return self._status

    def request_shutdown(self) -> None:
        """Idempotently wake the host and request cooperative cleanup."""
        self.stop_event.set()

    def mark_degraded(self) -> None:
        """Report optional impairment on the host thread without stopping it."""
        if self.status.state == Lifecycle.RUNNING:
            self._set_state(Lifecycle.DEGRADED)

    def _set_state(self, state: Lifecycle) -> None:
        self._status = RuntimeStatus(state, _SUMMARIES[state])

    def run(self) -> None:
        """Wait for cancellation; fatal failures retain error status and propagate."""
        if self._has_run:
            raise RuntimeError("Runtime host can only run once.")
        self._has_run = True
        try:
            self._set_state(Lifecycle.RUNNING)
            self.stop_event.wait()
            self._set_state(Lifecycle.STOPPING)
            # Future owned responsibilities perform bounded cleanup here.
            self._set_state(Lifecycle.STOPPED)
        except BaseException:
            self._set_state(Lifecycle.ERROR)
            self.request_shutdown()
            raise
