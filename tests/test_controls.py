"""Controls own only transient chrome; commands remain serialized by playback."""

from concurrent.futures import Future
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from postcardscene.graphics._capability import CapabilityError
from postcardscene.graphics.control_protocol import (
    Action,
    ControlState,
    buttons,
    decode_state,
)
from postcardscene.graphics.overlay_helper import Commands
from postcardscene.runtime.controls import Controls
from postcardscene.runtime.presentation import ContentSnapshot, Outcome, PlaybackStatus


class Endpoint:
    def __init__(self):
        self.states = []
        self.stopped = False
        self.started = Event()

    def start(self, **kwargs):
        self.started.set()

    def update(self, state, **kwargs):
        self.states.append(state)

    def receive(self, **kwargs):
        # Exercise cooperative waits without toolkit or compositor dependencies.
        if kwargs["cancelled"]():
            raise CapabilityError("cancelled")
        Event().wait(0.001)
        return None

    def stop(self):
        self.stopped = True


@pytest.fixture
def controls():
    submitted = []

    def submit(*command):
        submitted.append(command)
        result = Future()
        result.set_result(Outcome.OK)
        return result

    playback = SimpleNamespace(status=PlaybackStatus(), submit=submit)
    clock = SimpleNamespace(now=0.0)
    instance = Controls(
        playback, Endpoint(), Endpoint(), Event(), clock=lambda: clock.now
    )
    return instance, clock, submitted


def test_visibility_activity_and_suppression_never_change_pause(controls):
    owner, clock, submitted = controls
    owner.playback.status = PlaybackStatus(paused=True, state="paused")
    owner.refresh()
    assert owner.overlay.states[-1] == ControlState(enabled=True)
    owner.activity("activity")
    owner.refresh()
    assert owner.overlay.states[-1].visible and owner.overlay.states[-1].paused
    clock.now = 4.9
    owner.refresh()
    assert len(owner.overlay.states) == 2
    clock.now = 5.0
    owner.refresh()
    assert not owner.overlay.states[-1].visible
    owner.activity("next")
    owner.refresh()
    assert submitted == [("next", None)]
    owner.playback.status = replace(
        owner.playback.status, reason="unavailable", output_suppressed=True
    )
    owner.refresh()
    assert owner.overlay.states[-1] == ControlState()
    owner.activity("next")
    assert submitted == [("next", None)]
    owner.playback.status = replace(
        owner.playback.status, reason="ready", output_suppressed=False
    )
    owner.refresh()
    assert owner.overlay.states[-1] == ControlState(enabled=True)


def test_actions_capabilities_and_bounded_feedback(controls):
    owner, _, submitted = controls
    for action in Action:
        owner.activity(action)
    assert submitted == [("previous", None), ("toggle_pause", None), ("next", None)]
    video = ContentSnapshot(seekable=True, audio_available=True, muted=True, volume=95)
    owner.playback.status = PlaybackStatus(kind="video", video=video)
    for action in (
        Action.SEEK_BACK,
        Action.SEEK_FORWARD,
        Action.TOGGLE_MUTE,
        Action.VOLUME_DOWN,
        Action.VOLUME_UP,
    ):
        owner.activity(action)
    assert submitted[3:] == [
        ("seek_relative", -10),
        ("seek_relative", 10),
        ("unmute", None),
        ("set_volume", 85),
        ("set_volume", 100),
    ]
    pending = Future()
    owner.playback.submit = lambda *_: pending
    owner.activity("next")
    owner.activity("previous")
    assert owner.feedback == Outcome.BUSY
    pending.set_result(Outcome.UNAVAILABLE)
    owner.refresh()
    assert owner.feedback == Outcome.UNAVAILABLE
    owner.playback.status = PlaybackStatus(
        kind="video", video=replace(video, ended=True)
    )
    owner.activity("seek_forward_10")
    assert owner.feedback == Outcome.UNAVAILABLE
    with pytest.raises(ValueError):
        owner.activity("https://secret")


def test_progress_refresh_does_not_extend_visibility(controls):
    owner, clock, _ = controls
    owner.playback.status = PlaybackStatus(
        kind="video", video=ContentSnapshot(seekable=True)
    )
    owner.activity("activity")
    owner.refresh()
    for now in (1, 3, 5):
        clock.now = now
        owner.playback.status = replace(
            owner.playback.status,
            video=ContentSnapshot(seekable=True, position_seconds=now),
        )
        owner.refresh()
    assert not owner.overlay.states[-1].visible
    assert len(owner.overlay.states) == 2


def test_coordinator_shutdown_and_degraded_vs_uncertain_cleanup(controls):
    owner, _, _ = controls
    owner.start()
    assert owner.channel.started.wait(1)
    owner.join()
    assert owner.overlay.stopped and owner.channel.stopped
    assert owner.status.reason == "stopped" and not owner.stop_event.is_set()

    for uncertain in (False, True):
        overlay, channel = Endpoint(), Endpoint()

        def fail(**kwargs):
            error = CapabilityError("protocol_failed")
            error.cleanup_failed = uncertain
            raise error

        overlay.start = fail
        instance = Controls(owner.playback, overlay, channel, Event())
        instance.start()
        instance.thread.join(1)
        assert not instance.thread.is_alive()
        assert overlay.stopped and channel.stopped
        assert instance.stop_event.is_set() == uncertain
        assert instance.failure == ("cleanup_failed" if uncertain else None)
        assert instance.status.reason == (
            "cleanup_failed" if uncertain else "unavailable"
        )


def test_cleanup_exception_is_fatal_even_after_normal_cancellation(controls):
    owner, _, _ = controls

    def fail():
        raise OSError("private path")

    owner.overlay.stop = fail
    owner.start()
    assert owner.channel.started.wait(1)
    with pytest.raises(RuntimeError, match="^Controls cleanup_failed$"):
        owner.join()
    assert owner.channel.stopped and owner.stop_event.is_set()


def test_closed_state_and_stream_framing():
    state = ControlState(
        enabled=True,
        visible=True,
        paused=True,
        seekable=True,
        audio=True,
        muted=False,
        volume=100,
    )
    encoded = state.encode().encode("ascii")
    assert len(encoded) < 1024 and decode_state(encoded) == state
    for invalid in (
        b"x" * 1024,
        b'{"url":"https://secret"}',
        b"[]",
        encoded.replace(b"100", b"true"),
        encoded.replace(b"100", b"101"),
        encoded.replace(b"false", b'"secret"'),
        b'{"enabled":true,' + encoded[1:],
    ):
        with pytest.raises(ValueError, match="^invalid_state$"):
            decode_state(invalid)
    panel = SimpleNamespace(
        state=ControlState(), update=lambda value: setattr(panel, "state", value)
    )
    replies = []
    parser = Commands(panel, lambda: None, replies.append)
    assert parser.feed(encoded[:20])
    assert not replies
    assert parser.feed(encoded[20:] + b"\nhide\n")
    assert replies == ["updated", "hidden"] and not panel.state.visible
    assert not parser.feed(b"x" * 1024)
    assert not Commands(panel, lambda: None, replies.append).feed(b"raw-key\n")


def test_button_vocabulary_and_fixed_production_mapping():
    assert buttons(ControlState()) == [
        (Action.PREVIOUS, "Previous"),
        (Action.TOGGLE_PAUSE, "Pause"),
        (Action.NEXT, "Next"),
    ]
    assert buttons(ControlState(paused=True))[1][1] == "Play"
    assert {a for a, _ in buttons(ControlState(seekable=True, audio=True))} == set(
        Action
    ) - {Action.ACTIVITY}
    tree = ElementTree.parse(
        "src/postcardscene/graphics/labwc/playback-keybindings.xml"
    )
    expected = {
        "Left": "previous",
        "Right": "next",
        "space": "toggle_pause",
        "C-Left": "seek_back_10",
        "C-Right": "seek_forward_10",
        "m": "toggle_mute",
        "Up": "volume_up_10",
        "Down": "volume_down_10",
    }
    actual = {}
    for binding in tree.findall("keybind"):
        action = binding.find("action")
        assert action.attrib == {"name": "Execute"}
        executable, event = action.findtext("command").split()
        assert executable == "/opt/postcardscene/venv/bin/postcardscene-input-emitter"
        actual[binding.get("key")] = Action(event).value
    assert actual == expected
