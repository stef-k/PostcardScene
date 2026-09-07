"""Execution contracts with fake content authority, never a real renderer."""

from dataclasses import replace
from threading import Event, get_ident
from time import monotonic
from types import SimpleNamespace

import pytest

from postcardscene.display_planner import DisplayStep, PlanResult, PlanStatus
from postcardscene.playback_configuration import Eligibility
from postcardscene.runtime.playback import PlaybackWorker
from postcardscene.runtime.playback_state import PlaybackState
from postcardscene.runtime.presentation import (
    ContentSnapshot,
    Outcome,
    PresentationError,
)


def step(kind="image", identity=1):
    return DisplayStep(1, identity, identity, 1, kind, ((1, "private-photo.jpg"),))


class Planner:
    def __init__(self, steps):
        self.steps = iter(steps)
        self.calls = []
        self.valid = True

    def next(self):
        self.calls.append("next")
        return next(self.steps, PlanResult(PlanStatus.INELIGIBLE))

    def previous(self):
        self.calls.append("previous")
        return PlanResult(PlanStatus.HISTORY_BOUNDARY)

    def revalidate(self, current):
        self.calls.append("revalidate")
        return PlanResult(
            PlanStatus.READY if self.valid else PlanStatus.INELIGIBLE, current
        )


class Presenter:
    def __init__(self):
        self.calls = []
        self.threads = set()
        self.content = ContentSnapshot()
        self.show_error = None
        self.cleanup_error = False
        self.block = False
        self.entered = Event()
        self.stopped = Event()

    def record(self, name):
        self.calls.append(name)
        self.threads.add(get_ident())

    def show(self, step, *, start_paused, cancelled):
        self.record(("show", step.membership_id, start_paused))
        self.entered.set()
        if self.block:
            while not cancelled():
                Event().wait(0.001)
            self.block = False
            raise PresentationError(Outcome.CANCELLED)
        if self.show_error:
            raise PresentationError(self.show_error)
        return replace(self.content, active=True, ended=False)

    def snapshot(self, *, cancelled):
        self.record("snapshot")
        return self.content

    def clear(self, *, cancelled):
        self.record("clear")
        if self.cleanup_error:
            raise PresentationError(Outcome.CLEANUP_FAILED)

    def stop(self):
        self.record("stop")
        self.stopped.set()
        if self.cleanup_error:
            raise PresentationError(Outcome.CLEANUP_FAILED)

    def pause(self, *, cancelled):
        self.record("pause")
        return self.content

    def resume(self, *, cancelled):
        self.record("resume")
        return self.content

    def seek_relative(self, value, *, cancelled):
        self.record(("seek", value))
        return self.content

    def mute(self, *, cancelled):
        self.record("mute")
        return self.content

    unmute = mute

    def set_volume(self, value, *, cancelled):
        self.record(("volume", value))
        return self.content


class Configuration:
    def __init__(self, override=None, duration=None, default=30):
        self.eligibility = Eligibility.READY
        self.override = override
        self.duration = duration
        self.default = default
        self.reads = []

    def __call__(self, membership_id=None):
        self.reads.append(membership_id)
        return SimpleNamespace(
            eligibility=self.eligibility,
            sequence=SimpleNamespace(id=1),
            scene=SimpleNamespace(
                id=membership_id, widget_id=1, duration_seconds=self.duration
            ),
            membership=SimpleNamespace(duration_override_seconds=self.override),
            settings=SimpleNamespace(default_scene_dwell_seconds=self.default),
        )


class Harness:
    def __init__(self, kind="image", override=None, duration=None, default=30):
        self.now = 0.0
        self.planner = Planner(
            [PlanResult(PlanStatus.READY, step(kind, i)) for i in range(1, 40)]
        )
        self.presenter = Presenter()
        self.configuration = Configuration(override, duration, default)
        self.status = None
        self.machine = PlaybackState(
            self.planner,
            self.configuration,
            self.presenter,
            lambda: self.now,
            self.publish,
        )

    def publish(self, status):
        self.status = status

    def tick(self, seconds=0):
        self.now += seconds
        return self.machine.execute(self.machine.tick, lambda: False)

    def command(self, name, value=None):
        return self.machine.execute(
            lambda cancel: self.machine.command(name, value, cancel), lambda: False
        )


@pytest.mark.parametrize("kind", ["image", "portrait_image_pair", "web_view", "video"])
@pytest.mark.parametrize(
    "override,duration,expected", [(3, 7, 3), (None, 7, 7), (None, None, 30)]
)
def test_dwell_precedence_and_automatic_progression(kind, override, duration, expected):
    h = Harness(kind, override, duration)
    h.tick()
    if kind == "video" and override is None and duration is None:
        assert h.status.remaining_seconds is None
        assert h.tick(1000) == 0.5
        assert h.status.membership_id == 1
    else:
        assert h.status.remaining_seconds == expected
        h.tick(expected - 0.25)
        assert h.status.membership_id == 1
        h.tick(0.25)
        assert h.status.membership_id == 2


@pytest.mark.parametrize("duration", [None, 30])
def test_video_natural_eof_wins(duration):
    h = Harness("video", duration=duration)
    h.tick()
    h.presenter.content = ContentSnapshot(active=False, ended=True)
    h.tick(0.5)
    assert h.status.membership_id == 2


@pytest.mark.parametrize("kind", ["image", "web_view", "video"])
def test_pause_freezes_budget_and_resume_continues(kind):
    h = Harness(kind, duration=10)
    h.tick()
    h.tick(3)
    h.command("pause")
    h.tick(100)
    assert h.status.paused and h.status.remaining_seconds == 7
    assert ("pause" in h.presenter.calls) == (kind == "video")
    h.command("resume")
    h.tick(6)
    assert h.status.membership_id == 1
    h.tick(1)
    assert h.status.membership_id == 2


def test_navigation_delegates_and_new_video_starts_paused():
    h = Harness("video")
    h.tick()
    h.command("pause")
    assert h.command("previous") == Outcome.UNAVAILABLE
    assert h.planner.calls[-1] == "previous"
    assert h.status.membership_id == 1
    h.command("next")
    assert h.status.paused
    assert ("show", 2, True) in h.presenter.calls


def test_suppression_preserves_identity_pause_and_revalidates_on_release():
    h = Harness("video")
    h.tick()
    h.command("pause")
    h.command("set_output_suppressed", True)
    assert h.status.reason == "suppressed"
    assert h.status.membership_id == 1 and h.status.paused
    assert h.presenter.calls[-1] == "clear"
    count = len(h.planner.calls)
    assert h.tick(100) is None
    assert len(h.planner.calls) == count
    h.command("set_output_suppressed", False)
    h.tick()
    assert h.planner.calls[-1] == "revalidate"
    assert h.presenter.calls[-1] == ("show", 1, True)
    assert h.status.paused
    h.command("set_output_suppressed", True)
    h.planner.valid = False
    h.command("set_output_suppressed", False)
    h.tick()
    assert h.machine.current is None
    assert h.status.reason == "unavailable"


def test_eight_failures_backoff_and_success_reset():
    h = Harness()
    h.presenter.show_error = Outcome.UNAVAILABLE
    for _ in range(8):
        h.tick()
    assert len([c for c in h.presenter.calls if isinstance(c, tuple)]) == 8
    assert h.presenter.calls[-1] == "clear"
    assert h.status.state == "degraded"
    assert h.tick() == 5
    assert h.tick(4) == 1
    h.presenter.show_error = None
    h.tick(1)
    assert h.status.membership_id == 9
    assert h.machine.failures == 0
    h.presenter.show_error = Outcome.UNAVAILABLE
    h.command("next")
    assert h.machine.failures == 1


@pytest.mark.parametrize(
    "eligibility,state",
    [(Eligibility.IDLE, "idle"), (Eligibility.SEQUENCE_DISABLED, "degraded")],
)
def test_no_active_or_disabled_configuration_backs_off(eligibility, state):
    h = Harness()
    h.planner = h.machine.planner = Planner([])
    h.configuration.eligibility = eligibility
    h.tick()
    assert h.status.state == state
    assert h.tick() == 5
    h.tick(5)
    assert h.planner.calls == ["next", "next"]


def test_no_candidate_and_planner_failure_are_bounded():
    h = Harness()
    h.machine.planner = Planner([PlanResult(PlanStatus.FAILURE)] * 4)
    for _ in range(8):
        h.tick()
    assert h.tick() == 5


@pytest.mark.parametrize("kind", ["image", "web_view", "video"])
def test_capability_commands_are_unavailable_without_capability(kind):
    h = Harness(kind)
    h.tick()
    for name, value in [("seek_relative", 10), ("mute", None), ("set_volume", 50)]:
        assert h.command(name, value) == Outcome.UNAVAILABLE
    assert h.presenter.calls[-1][0] == "show"


def test_video_capability_commands_and_status_are_safe():
    h = Harness("video")
    h.presenter.content = ContentSnapshot(
        seekable=True,
        audio_available=True,
        position_seconds=2,
        duration_seconds=20,
        volume=50,
    )
    h.tick()
    assert h.command("seek_relative", -10) == Outcome.OK
    assert h.command("mute") == Outcome.OK
    assert h.command("set_volume", 100) == Outcome.OK
    assert h.status.video.position_seconds == 2
    assert "private-photo" not in repr(h.status)
    h.presenter.content = replace(
        h.presenter.content, position_seconds="https://secret", volume="/private"
    )
    h.tick(0.5)
    assert h.status.video.position_seconds is None and h.status.video.volume is None
    assert "secret" not in repr(h.status)


def make_worker(presenter, **kwargs):
    return PlaybackWorker(
        None,
        presenter,
        Event(),
        planner=Planner([PlanResult(PlanStatus.READY, step("video"))]),
        configuration=Configuration(),
        **kwargs,
    )


def test_thread_serializes_cancellation_and_shutdown():
    presenter = Presenter()
    presenter.block = True
    worker = make_worker(presenter)
    worker.start()
    try:
        assert presenter.entered.wait(2)
        assert worker.submit("pause").result(2) == Outcome.OK
        assert worker.submit("mute").result(2) == Outcome.UNAVAILABLE
        assert worker.status.paused
        assert worker._machine.failures == 0
    finally:
        started = monotonic()
        worker.join()
    assert monotonic() - started < 5
    assert presenter.stopped.is_set()
    assert presenter.threads == {worker.thread.ident}
    assert worker.thread.ident != get_ident()
    assert worker.status.state == "stopped" and worker.failure is None


def test_mailbox_bounded_and_invalid_commands_rejected():
    worker = make_worker(Presenter())
    for name, value in [
        ("seek_relative", float("nan")),
        ("set_volume", True),
        ("set_volume", 101),
        ("set_output_suppressed", 1),
        ("bad", None),
    ]:
        assert worker.submit(name, value).result() == Outcome.REJECTED
    queued = [worker.submit("next") for _ in range(32)]
    assert worker.submit("pause").result() == Outcome.BUSY
    worker.request_shutdown()
    worker.start()
    worker.join()
    assert all(f.result() == Outcome.REJECTED for f in queued)


def test_cleanup_uncertainty_is_fatal_and_wakes_host():
    presenter = Presenter()
    presenter.show_error = Outcome.CLEANUP_FAILED
    worker = make_worker(presenter)
    worker.start()
    assert worker.stop_event.wait(2)
    with pytest.raises(RuntimeError, match="cleanup_failed"):
        worker.join()
    assert worker.failure == "cleanup_failed"
    assert worker.status.state == "error"
    assert worker._machine.planner.calls.count("next") == 1
    assert presenter.calls[-1] == "stop"


def test_unexpected_backend_exception_is_sanitized():
    presenter = Presenter()

    def show(*args, **kwargs):
        raise RuntimeError("https://secret.example /private/path")

    presenter.show = show
    worker = make_worker(presenter)
    worker.start()
    assert worker.stop_event.wait(2)
    with pytest.raises(RuntimeError, match="worker_failed"):
        worker.join()
    assert "secret" not in repr(worker.status)
    assert worker.failure == "worker_failed"


def test_repeated_suppression_release_keeps_playing_status():
    h = Harness()
    h.tick()
    h.command("set_output_suppressed", False)
    h.tick()
    assert h.status.state == "playing"


def test_already_ended_show_is_bounded_as_unavailable():
    h = Harness("video")
    h.presenter.show = lambda *args, **kwargs: ContentSnapshot(active=False, ended=True)
    for _ in range(8):
        h.tick()
    assert h.tick() == 5
    assert h.status.state == "degraded"


def test_previous_replay_keeps_pause_and_rereads_duration():
    h = Harness("web_view")
    h.tick()
    h.command("next")
    h.command("pause")
    h.machine.planner.previous = lambda: PlanResult(PlanStatus.READY, step("web_view"))
    h.configuration.override = 12
    h.command("previous")
    assert h.status.membership_id == 1
    assert h.status.remaining_seconds == 12 and h.status.paused
    assert h.configuration.reads[-1] == 1


def test_clear_failure_never_shows_replacement():
    h = Harness()
    h.tick()
    h.presenter.cleanup_error = True
    with pytest.raises(PresentationError) as error:
        h.command("next")
    assert error.value.outcome == Outcome.CLEANUP_FAILED
    assert len([c for c in h.presenter.calls if isinstance(c, tuple)]) == 1


def test_worker_injected_wait_advances_timed_content_without_polling():
    presenter = Presenter()
    now = [0.0]
    waits = []
    stop = Event()

    def wait(condition, timeout):
        waits.append(timeout)
        now[0] += timeout
        if len([c for c in presenter.calls if isinstance(c, tuple)]) == 2:
            stop.set()

    worker = PlaybackWorker(
        None,
        presenter,
        stop,
        planner=Planner(
            [PlanResult(PlanStatus.READY, step("web_view", i)) for i in (1, 2)]
        ),
        configuration=Configuration(default=12),
        clock=lambda: now[0],
        wait=wait,
    )
    worker.start()
    assert presenter.stopped.wait(2)
    worker.join()
    assert waits == [0.0, 12.0, 0.0]
    assert "snapshot" not in presenter.calls


def test_join_wakes_shared_stop_during_long_dwell():
    presenter = Presenter()
    worker = PlaybackWorker(
        None,
        presenter,
        Event(),
        planner=Planner([PlanResult(PlanStatus.READY, step())]),
        configuration=Configuration(default=86400),
    )
    worker.start()
    assert presenter.entered.wait(2)
    worker.stop_event.set()
    worker.join()
    assert worker.status.state == "stopped"


def test_uncooperative_operation_reports_fatal_join_timeout(monkeypatch):
    from postcardscene.runtime import playback

    presenter = Presenter()
    release = Event()

    def show(*args, **kwargs):
        presenter.entered.set()
        release.wait(2)
        return ContentSnapshot()

    presenter.show = show
    worker = make_worker(presenter)
    monkeypatch.setattr(playback, "JOIN_SECONDS", 0.02)
    worker.start()
    assert presenter.entered.wait(2)
    try:
        with pytest.raises(RuntimeError, match="shutdown_timeout"):
            worker.join()
        assert worker.status.state == "error"
        assert worker.stop_event.is_set()
    finally:
        release.set()
        worker.thread.join(2)
    assert not worker.thread.is_alive()
    assert worker.failure == "shutdown_timeout"
