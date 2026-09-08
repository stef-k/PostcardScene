"""Transport-independent runtime lifecycle; no web or hardware initialization."""

import logging
from dataclasses import dataclass
from enum import StrEnum
from threading import Event

from postcardscene.graphics import WaylandSession
from postcardscene.graphics.mutation import DisplayMutationGuard
from postcardscene.power import CecBackend, DdcBackend, SignalBackend
from postcardscene.runtime.catalog_refresh import CatalogRefreshWorker
from postcardscene.runtime.output import OutputMonitor
from postcardscene.runtime.panel import PanelCoordinator
from postcardscene.runtime.panel_control import PanelControl, panel_status


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
    Panel intent is separate from this lifecycle; shutdown never requests power.
    """

    def __init__(self, database=None, policy=None, config=None):
        self.stop_event = Event()
        self._status = RuntimeStatus(Lifecycle.STARTING, _SUMMARIES[Lifecycle.STARTING])
        self._has_run = False
        if (database is None) != (policy is None):
            raise ValueError("Supply both Database and PathPolicy.")
        self.catalog_worker = (
            CatalogRefreshWorker(database, policy, self.stop_event)
            if database is not None
            else None
        )

        self.output_monitor = None
        mutation_guard = DisplayMutationGuard()
        session = WaylandSession() if config is not None else None
        self.panel_coordinator = None
        self.panel_control = None
        if config is not None and config.get("PANEL_POWER_RUNTIME_ENABLED", False):
            if database is None:
                raise ValueError("Panel runtime requires Database.")
            self.panel_coordinator = PanelCoordinator(
                database,
                self.stop_event,
                cec=CecBackend(config.get("CEC_DEVICE")),
                ddc=DdcBackend(config.get("DDC_DISPLAY")),
                signal=SignalBackend(
                    session,
                    connector_override=config.get("DISPLAY_CONNECTOR"),
                    mutation_guard=mutation_guard,
                ),
            )
            self.panel_control = PanelControl(self.panel_coordinator, self.stop_event)

        if config is not None:
            self.output_monitor = OutputMonitor(
                session,
                self.stop_event,
                mutation_guard,
                connector_override=config.get("DISPLAY_CONNECTOR"),
                panel_coordinator=self.panel_coordinator,
            )

    @property
    def display_status(self):
        return self.output_monitor.status.display if self.output_monitor else None

    @property
    def output_monitor_status(self):
        return self.output_monitor.status if self.output_monitor else None

    @property
    def panel_status(self):
        return panel_status(self.panel_coordinator)

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
        started = []
        try:
            try:
                if self.catalog_worker is not None:
                    self.catalog_worker.start()
                    started.append(self.catalog_worker)
                if self.panel_coordinator is not None:
                    # Reserve local ownership before any hardware work can run.
                    self.panel_control.start()
                    self.panel_coordinator.start()
                    started.append(self.panel_coordinator)
                if self.output_monitor is not None:
                    self.output_monitor.start()
                    started.append(self.output_monitor)
                self._set_state(Lifecycle.RUNNING)
                logging.getLogger("postcardscene.runtime.lifecycle").info(
                    "Runtime entering normal service operation."
                )
                self.stop_event.wait()
            except BaseException as error:
                try:
                    self._stop_components(started)
                except BaseException:
                    error.add_note("Runtime component cleanup also failed.")
                raise
            else:
                self._stop_components(started)
            self._set_state(Lifecycle.STOPPED)
        except BaseException:
            self._set_state(Lifecycle.ERROR)
            self.request_shutdown()
            raise

    def _stop_components(self, started):
        self.request_shutdown()
        self._set_state(Lifecycle.STOPPING)
        logging.getLogger("postcardscene.runtime.lifecycle").info(
            "Runtime shutdown beginning."
        )
        failure = None
        # Always attempt remaining cleanup, preserving the first failure.
        cleanup = []
        if self.panel_control is not None:
            cleanup.append(self.panel_control.stop)
        cleanup.extend(component.join for component in reversed(started))
        for stop in cleanup:
            try:
                stop()
            except BaseException as error:
                if failure is None:
                    failure = error
                else:
                    failure.add_note("Another runtime component cleanup failed.")
        if failure is not None:
            raise failure
        if self.catalog_worker is not None and self.catalog_worker.failure is not None:
            raise RuntimeError(
                "Catalog worker failed."
            ) from self.catalog_worker.failure
