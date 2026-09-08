"""Runtime lifecycle and controlled signal/output mutation interleavings."""

from dataclasses import FrozenInstanceError
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from postcardscene.graphics import WaylandSession
from postcardscene.graphics.mutation import DisplayMutationGuard
from postcardscene.graphics.output import DisplayStatus
from postcardscene.power import SignalBackend, State
from postcardscene.runtime import Lifecycle, RuntimeHost
from postcardscene.runtime import output


def test_configured_host_shares_one_session_guard_and_selector(catalog):
    host = RuntimeHost(
        catalog[0],
        catalog[3],
        {
            "PANEL_POWER_RUNTIME_ENABLED": True,
            "DISPLAY_CONNECTOR": "HDMI-A-2",
        },
    )
    monitor = host.output_monitor
    signal = host.panel_coordinator._backends[2]
    assert isinstance(monitor.session, WaylandSession)
    assert signal._session is monitor.session
    assert signal.mutation_guard is monitor.mutation_guard
    assert signal._connector_override == monitor.connector_override == "HDMI-A-2"
    assert monitor.stop_event is host.stop_event
    assert monitor.panel_coordinator is host.panel_coordinator
    assert all(
        not hasattr(b, "mutation_guard") for b in host.panel_coordinator._backends[:2]
    )
    assert RuntimeHost().output_monitor is None
    assert RuntimeHost().display_status is None


def test_monitor_lifecycle_degradation_and_retained_snapshot(monkeypatch):
    host = RuntimeHost(config={"DISPLAY_CONNECTOR": "HDMI-A-2"})
    monitor = host.output_monitor
    panel = SimpleNamespace(intentional_signal_sleep=False)
    monitor.panel_coordinator = panel
    snapshot = DisplayStatus(True, "no_display", "no_display")
    calls = []

    def reconcile(session, **kwargs):
        assert session is monitor.session
        assert kwargs == {
            "connector_override": "HDMI-A-2",
            "stop_event": host.stop_event,
        }
        calls.append("reconcile")
        return snapshot

    waits = []

    def wait(timeout=None):
        # This is the actual #63 cadence. No display lock spans the wait.
        assert timeout == 1.0
        assert monitor.mutation_guard.acquire(lambda: False)
        monitor.mutation_guard.release()
        waits.append(timeout)
        if len(waits) == 1:
            assert host.display_status is snapshot
            assert not host.stop_event.is_set()
            panel.intentional_signal_sleep = True
        elif len(waits) == 2:
            assert host.output_monitor_status.state == "suspended"
            assert host.output_monitor_status.reason == "intentional_signal_sleep"
            assert host.display_status is snapshot
            panel.intentional_signal_sleep = False
        else:
            host.request_shutdown()
        return host.stop_event.is_set()

    monkeypatch.setattr(output, "reconcile_display", reconcile)
    monkeypatch.setattr(host.stop_event, "wait", wait)
    monitor.start()
    monitor.join()
    assert calls == ["reconcile", "reconcile"]
    assert monitor.status.state == "stopped"
    with pytest.raises(FrozenInstanceError):
        host.display_status.state = "ready"


@pytest.mark.parametrize("signal_first", [False, True])
def test_signal_and_monitor_serialize_and_recheck_after_acquire(
    monkeypatch, signal_first
):
    guard = DisplayMutationGuard()
    stop = Event()
    panel = SimpleNamespace(intentional_signal_sleep=False)
    monitor = output.OutputMonitor(object(), stop, guard, panel_coordinator=panel)
    backend = SignalBackend(object(), mutation_guard=guard)
    entered, release, waiting = Event(), Event(), Event()
    mutations = []

    def reconcile(*args, **kwargs):
        mutations.append("reconcile")
        entered.set()
        assert release.wait(2)
        return DisplayStatus(True, "ready", "ready")

    def signal_operation(requested, cancelled):
        if signal_first:
            entered.set()
            assert release.wait(2)
        mutations.append(requested)
        return requested

    monkeypatch.setattr(output, "reconcile_display", reconcile)
    monkeypatch.setattr(backend, "_operate_guarded", signal_operation)
    original_acquire = guard.acquire

    def acquire(cancelled):
        waiting.set()
        return original_acquire(cancelled)

    results = []
    first = Thread(
        target=(lambda: results.append(backend.request_off(stop.is_set)))
        if signal_first
        else monitor.reconcile
    )
    second = Thread(
        target=monitor.reconcile
        if signal_first
        else lambda: results.append(backend.request_off(stop.is_set))
    )
    try:
        first.start()
        assert entered.wait(2)
        monkeypatch.setattr(guard, "acquire", acquire)
        second.start()
        assert waiting.wait(2)
        # This changes AFTER the monitor began waiting when signal won.
        panel.intentional_signal_sleep = True
        release.set()
        first.join(2)
        second.join(2)
        assert not first.is_alive() and not second.is_alive()
        assert mutations == ([State.OFF] if signal_first else ["reconcile", State.OFF])
        assert results[0].signal == State.OFF
        # Wake readback alone does not authorize a waiting monitor to reconcile.
        backend.request_on(stop.is_set)
        assert monitor.reconcile() is None
        panel.intentional_signal_sleep = False
        assert monitor.reconcile().state == "ready"
    finally:
        stop.set()
        release.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)


@pytest.mark.parametrize("operation", ["probe", "observe", "request_on", "request_off"])
def test_signal_guard_cancellation_issues_no_operation(monkeypatch, operation):
    guard = DisplayMutationGuard()
    backend = SignalBackend(object(), mutation_guard=guard)
    cancelled, waiting = Event(), Event()
    monkeypatch.setattr(
        backend, "_operate_guarded", lambda *a: pytest.fail("operation")
    )
    assert guard.acquire(lambda: False)
    results = []

    def predicate():
        waiting.set()
        return cancelled.is_set()

    thread = Thread(
        target=lambda: results.append(getattr(backend, operation)(predicate))
    )
    try:
        thread.start()
        assert waiting.wait(2)
        cancelled.set()
        thread.join(0.5)
        assert not thread.is_alive()
        assert results[0].status == "cancelled"
    finally:
        guard.release()
        thread.join(2)


def test_monitor_cancelled_while_waiting_does_not_reconcile(monkeypatch):
    host = RuntimeHost(config={})
    guard = host.output_monitor.mutation_guard
    monkeypatch.setattr(
        output, "reconcile_display", lambda *a, **kw: pytest.fail("reconcile")
    )
    assert guard.acquire(lambda: False)
    try:
        host.output_monitor.start()
        host.request_shutdown()
        host.output_monitor.join()
        assert host.output_monitor.status.state == "stopped"
    finally:
        guard.release()


def test_monitor_failure_propagates_and_other_workers_join(catalog, monkeypatch):
    host = RuntimeHost(catalog[0], catalog[3], {})

    def fail(*args, **kwargs):
        raise RuntimeError("private failure")

    monkeypatch.setattr(output, "reconcile_display", fail)
    with pytest.raises(RuntimeError, match="Output monitor failed"):
        host.run()
    assert host.stop_event.is_set()
    assert host.status.state == Lifecycle.ERROR
    assert host.output_monitor.status.reason == "worker_failed"
    assert not host.catalog_worker.thread.is_alive()


def test_startup_and_cleanup_order(catalog, monkeypatch):
    host = RuntimeHost(catalog[0], catalog[3], {"PANEL_POWER_RUNTIME_ENABLED": True})
    calls = []
    for name, component in [
        ("catalog", host.catalog_worker),
        ("control", host.panel_control),
        ("panel", host.panel_coordinator),
        ("output", host.output_monitor),
    ]:
        monkeypatch.setattr(component, "start", lambda name=name: calls.append(name))
        method = "stop" if name == "control" else "join"

        def cleanup(name=name):
            assert host.stop_event.is_set()
            calls.append("stop " + name)

        monkeypatch.setattr(component, method, cleanup)
    monkeypatch.setattr(host.stop_event, "wait", host.request_shutdown)
    host.run()
    assert calls == [
        "catalog",
        "control",
        "panel",
        "output",
        "stop control",
        "stop output",
        "stop panel",
        "stop catalog",
    ]


def test_output_join_uncertainty_is_fatal_and_cleanup_continues(catalog, monkeypatch):
    host = RuntimeHost(catalog[0], catalog[3], {})
    monitor = host.output_monitor
    bounds = []
    monkeypatch.setattr(monitor.thread, "start", lambda: None)
    monkeypatch.setattr(monitor.thread, "join", bounds.append)
    monkeypatch.setattr(monitor.thread, "is_alive", lambda: True)
    monkeypatch.setattr(host.stop_event, "wait", host.request_shutdown)
    with pytest.raises(RuntimeError, match="cleanup bound"):
        host.run()
    assert bounds == [5.0]
    assert host.status.state == Lifecycle.ERROR
    assert monitor.status.reason == "cleanup_failed"
    assert not host.catalog_worker.thread.is_alive()
