from dataclasses import FrozenInstanceError
from threading import Event
from time import monotonic

import pytest

from postcardscene.persistence import Database
from postcardscene.power import Kind, PowerResult, Reason, State, Status
from postcardscene.runtime import panel
from postcardscene.runtime.panel import Outcome, PanelCoordinator
from postcardscene.schema import upgrade_database
from postcardscene.settings import set_display_power_settings


class Backend:
    def __init__(self, kind, state=State.ON):
        self.kind, self.state = kind, state
        self.calls = []
        self.failure = None
        self.hook = None

    def run(self, method, cancelled):
        self.calls.append(method)
        if self.hook:
            self.hook(method, cancelled)
        if self.failure:
            return PowerResult(
                self.kind,
                self.failure,
                Reason.UNAVAILABLE,
                cleanup_failed=self.failure == Status.CLEANUP_FAILED,
            )
        if method in {"on", "off"}:
            self.state = State.ON if method == "on" else State.OFF
        signal = self.kind == Kind.SIGNAL
        return PowerResult(
            self.kind,
            Status.SIGNAL_ONLY if signal else Status.PHYSICAL,
            Reason.SIGNAL_ONLY if signal else Reason.CONFIRMED,
            physical=State.UNKNOWN if signal else self.state,
            signal=self.state if signal else State.UNKNOWN,
        )

    def probe(self, cancelled):
        return self.run("probe", cancelled)

    def observe(self, cancelled):
        return self.run("observe", cancelled)

    def request_on(self, cancelled):
        return self.run("on", cancelled)

    def request_off(self, cancelled):
        return self.run("off", cancelled)


@pytest.fixture
def rig(tmp_path):
    db = Database(tmp_path / "panel.sqlite3")
    upgrade_database(db.path)
    set_display_power_settings(db, "auto", 0, 1800)
    backends = [Backend(kind) for kind in Kind]
    worker = PanelCoordinator(
        db, Event(), cec=backends[0], ddc=backends[1], signal=backends[2]
    )
    yield worker, backends, db
    if worker.thread.ident is not None:
        worker.stop_event.set()
        worker.thread.join(2)
    db.engine.dispose()


def test_startup_idempotence_and_real_event_wakeup(rig):
    worker, (cec, ddc, signal), _ = rig
    worker.start()
    assert worker.apply_operating(True).result(2) == Outcome.READY
    assert worker.apply_operating(False).result(2) == Outcome.READY
    assert worker.apply_operating(False).result(2) == Outcome.READY
    assert worker.apply_operating(True).result(2) == Outcome.READY
    assert [c for c in cec.calls if c in {"on", "off"}] == ["off", "on"]
    assert not ddc.calls and not signal.calls
    with pytest.raises(FrozenInstanceError):
        worker.status.physical = State.OFF
    started = monotonic()
    worker.join()
    assert monotonic() - started < 5
    assert worker.status.state == "stopped"


def test_closed_hierarchy_and_fixed_diagnostic_expiry(rig):
    worker, (cec, _, _), _ = rig
    now = [0.0]
    worker._clock = lambda: now[0]
    worker._tick()
    worker.apply_operating(False)
    worker._tick()
    worker.request_test(True)
    worker._tick()
    assert worker.status.physical == State.ON
    worker.set_protection(True)
    worker._tick()
    assert worker.request_test(True).result() == Outcome.REJECTED
    worker.apply_operating(True)
    worker._tick()
    assert worker.status.physical == State.OFF
    worker.request_test(False)
    worker.set_protection(False)
    worker._tick()
    assert worker.status.physical == State.OFF
    now[0] = 4.99
    worker._tick()
    assert worker.status.physical == State.OFF
    now[0] = 5.0
    worker._tick()
    assert worker.status.physical == State.ON
    assert worker.status.diagnostic_active is None
    assert [c for c in cec.calls if c in {"on", "off"}] == ["off", "on", "off", "on"]


@pytest.mark.parametrize("choice,selected", [("cec", Kind.CEC), ("auto", Kind.DDC)])
def test_strict_selection_and_auto_priority(rig, choice, selected):
    worker, (cec, ddc, signal), db = rig
    set_display_power_settings(db, choice, 0, 1800)
    cec.failure = Status.UNAVAILABLE
    worker._tick()
    assert worker.status.active_backend == selected
    assert not signal.calls
    assert bool(ddc.calls) == (choice == "auto")
    assert worker.status.state == ("ready" if choice == "auto" else "degraded")


def test_signal_truth_and_sleeping_backend_change(rig):
    worker, (cec, ddc, signal), db = rig
    worker.apply_operating(False)
    worker._tick()
    set_display_power_settings(db, "signal", 0, 1800)
    worker._tick()
    assert not signal.calls
    worker.apply_operating(True)
    worker._tick()
    assert cec.state == State.ON and not signal.calls
    worker._tick()
    assert worker.status.active_backend == Kind.SIGNAL
    assert worker.status.physical == State.UNKNOWN
    assert worker.status.signal == State.ON
    assert worker.status.evidence == "signal_only"
    assert not ddc.calls


def test_unknown_owner_retries_without_fallback_and_fatal_cleanup(rig):
    worker, (cec, ddc, signal), _ = rig
    cec.failure = Status.DEGRADED
    worker._tick()
    worker._tick()
    assert worker.status.state == "degraded"
    assert not ddc.calls and not signal.calls
    cec.failure = None
    worker._tick()
    assert worker.status.state == "ready"
    cec.failure = Status.CLEANUP_FAILED
    worker._tick()
    assert worker.stop_event.is_set()
    assert worker.status.state == "error"
    assert not ddc.calls and not signal.calls


def test_wake_handshake_and_diagnostic_expiry_during_delay(rig):
    worker, (cec, _, _), db = rig
    now = [0.0]
    worker._clock = lambda: now[0]
    set_display_power_settings(db, "cec", 10, 1800)
    worker._tick()  # Already on: no delay.
    assert worker.status.state == "ready"
    worker.apply_operating(False)
    worker._tick()
    result = worker.request_test(True)
    worker._tick()
    assert not result.done() and worker.status.reason == "wake_delay"
    now[0] = 5
    worker._tick()
    assert result.result() == Outcome.SUPERSEDED
    assert worker.status.physical == State.OFF
    result = worker.apply_operating(True)
    worker._tick()
    now[0] = 14
    worker._tick()
    assert not result.done()
    now[0] = 15
    worker._tick()
    assert result.result() == Outcome.READY
    assert cec.calls.count("on") == 2


def test_settings_failure_holds_last_state_and_redacts(rig, monkeypatch):
    worker, (cec, _, _), _ = rig
    worker._tick()
    calls = list(cec.calls)
    pending = worker.apply_operating(False)
    original = panel.get_display_power_settings

    def fail(_):
        raise RuntimeError("secret /device/path")

    monkeypatch.setattr(panel, "get_display_power_settings", fail)
    worker._tick()
    assert pending.result() == Outcome.DEGRADED
    assert cec.calls == calls
    assert worker.status.desired_active is True
    assert "secret" not in repr(worker.status)
    monkeypatch.setattr(panel, "get_display_power_settings", original)
    worker._tick()
    assert worker.status.physical == State.OFF


def test_poll_budget_and_shutdown_during_handshake(rig):
    worker, (cec, _, _), db = rig
    deadlines = []
    worker._clock = lambda: 100

    def wait(deadline):
        deadlines.append(deadline)
        worker.stop_event.set()

    worker._wait = wait
    worker.start()
    worker.thread.join(2)
    worker.join()
    assert deadlines == [115]
    assert worker.status.state == "stopped"
    assert cec.calls == ["probe"]


def test_shutdown_cancels_hardware_and_pending_generation(rig):
    worker, (cec, _, _), _ = rig
    entered = Event()

    def block(method, cancelled):
        entered.set()
        while not cancelled():
            Event().wait(0.005)

    cec.hook = block
    pending = worker.apply_operating(False)
    worker.start()
    assert entered.wait(2)
    worker.join()
    assert pending.result() == Outcome.STOPPED
    assert cec.calls == ["probe"]


def test_new_intent_cancels_inflight_generation(rig):
    worker, (cec, _, _), _ = rig
    first = worker.apply_operating(False)

    def change(method, cancelled):
        cec.hook = None
        worker.set_protection(True)
        assert cancelled()

    cec.hook = change
    worker._tick()
    assert first.result() == Outcome.SUPERSEDED
    assert cec.calls == ["probe"]
    worker._tick()
    assert worker.status.physical == State.OFF


def test_lost_sleep_readback_retains_authority_and_can_wake(rig):
    worker, (cec, ddc, signal), db = rig
    worker._tick()

    def lose_readback(method, cancelled):
        if method == "off":
            cec.failure = Status.DEGRADED

    cec.hook = lose_readback
    worker.apply_operating(False)
    worker._tick()
    set_display_power_settings(db, "signal", 0, 1800)
    cec.failure = Status.UNAVAILABLE

    def recover(method, cancelled):
        if method == "on":
            cec.failure = None

    cec.hook = recover
    worker.apply_operating(True)
    worker._tick()
    assert worker.status.physical == State.ON
    assert cec.calls[-2:] == ["observe", "on"]
    assert not ddc.calls and not signal.calls


def test_fatal_join_and_precancelled_start(rig, monkeypatch):
    worker, (cec, _, _), _ = rig
    entered, release = Event(), Event()

    def block(method, cancelled):
        entered.set()
        release.wait(2)

    cec.hook = block
    monkeypatch.setattr(panel, "JOIN_SECONDS", 0.01)
    worker.start()
    try:
        assert entered.wait(2)
        with pytest.raises(RuntimeError, match="shutdown_timeout"):
            worker.join()
        assert worker.stop_event.is_set()
    finally:
        release.set()
        worker.thread.join(2)
    assert worker.status.state == "error"


def test_shutdown_interrupts_wake_delay(rig):
    worker, (cec, _, _), db = rig
    set_display_power_settings(db, "cec", 30, 1800)
    cec.state = State.OFF
    pending = worker.apply_operating(True)
    awake = Event()

    def notify(method, cancelled):
        if method == "on":
            awake.set()

    cec.hook = notify
    worker.start()
    assert awake.wait(2)
    worker.join()
    assert pending.result() == Outcome.STOPPED
    assert cec.calls.count("on") == 1


def test_unavailable_uncommanded_backend_can_be_explicitly_replaced(rig):
    worker, (cec, _, signal), db = rig
    set_display_power_settings(db, "cec", 0, 1800)
    cec.failure = Status.UNAVAILABLE
    worker._tick()
    set_display_power_settings(db, "signal", 0, 1800)
    worker._tick()
    assert worker.status.active_backend == Kind.SIGNAL
    assert signal.calls == ["probe"]


def test_wake_readback_cancelled_by_intent_keeps_handshake(rig):
    worker, (cec, _, _), db = rig
    set_display_power_settings(db, "cec", 5, 1800)
    now = [0.0]
    worker._clock = lambda: now[0]
    cec.state = State.OFF

    def interrupt(method, cancelled):
        if method == "on":
            worker.apply_operating(True)
            cec.hook = None
            assert cancelled()

    cec.hook = interrupt
    worker._tick()
    worker._tick()
    assert worker.status.reason == "wake_delay"
    now[0] = 5
    worker._tick()
    assert worker.status.state == "ready"
    assert cec.calls.count("on") == 1


def test_pass_deadline_degrades_without_switching_owners(rig):
    worker, (cec, ddc, signal), _ = rig
    now = [0.0]
    worker._clock = lambda: now[0]
    pending = worker.apply_operating(True)

    def timeout(method, cancelled):
        now[0] = 15
        assert cancelled()

    cec.hook = timeout
    worker._tick()
    assert pending.result() == Outcome.DEGRADED
    assert worker.status.reason == Reason.TIMEOUT
    assert not ddc.calls and not signal.calls


def test_precancelled_start_has_no_backend_calls(rig):
    worker, backends, _ = rig
    worker.stop_event.set()
    worker.start()
    worker.join()
    assert all(not backend.calls for backend in backends)
    assert worker.apply_operating(True).result() == Outcome.STOPPED


def test_diagnostic_expiry_during_io_reconverges_immediately(rig):
    worker, (cec, _, _), _ = rig
    now = [0.0]
    worker._clock = lambda: now[0]
    worker.apply_operating(False)
    worker.request_test(True)

    def expire(method, cancelled):
        now[0] = 5.0
        cec.hook = None
        assert cancelled()

    cec.hook = expire
    deadlines = []

    def wait(deadline):
        deadlines.append(deadline)
        if len(deadlines) == 2:
            worker.stop_event.set()

    worker._wait = wait
    worker.start()
    worker.thread.join(2)
    worker.join()
    assert deadlines == [5.0, 20.0]
    assert cec.state == State.OFF
