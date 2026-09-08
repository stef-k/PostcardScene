"""Transient chrome/input owner. PlaybackWorker remains the only transport owner."""

from dataclasses import replace
from threading import Event, Lock, Thread
from time import monotonic

from postcardscene.graphics._capability import CapabilityError, CapabilityStatus
from postcardscene.graphics.control_protocol import Action, ControlState
from postcardscene.runtime.presentation import Outcome

AUTO_HIDE_SECONDS = 5.0


def control_state(status):
    enabled = status.reason != "suppressed" and status.state not in {
        "stopping",
        "stopped",
        "error",
    }
    video = status.video if status.kind == "video" else None
    active = video is not None and video.active and not video.ended
    return ControlState(
        enabled=enabled,
        paused=status.paused,
        seekable=bool(active and video.seekable),
        audio=bool(active and video.audio_available),
        muted=video.muted if active else None,
        volume=video.volume if active else None,
    )


def playback_command(action, state):
    if action in (Action.PREVIOUS, Action.TOGGLE_PAUSE, Action.NEXT):
        return action.value, None
    if action in (Action.SEEK_BACK, Action.SEEK_FORWARD) and state.seekable:
        return "seek_relative", -10 if action == Action.SEEK_BACK else 10
    if state.audio:
        if action == Action.TOGGLE_MUTE and state.muted is not None:
            return ("unmute" if state.muted else "mute"), None
        if (
            action in (Action.VOLUME_DOWN, Action.VOLUME_UP)
            and state.volume is not None
        ):
            delta = -10 if action == Action.VOLUME_DOWN else 10
            return "set_volume", max(0, min(100, state.volume + delta))
    return None


class Controls:
    """Start once and join on host cancellation; no RuntimeHost wiring until #93.

    One thread alternates bounded input sources. Only it touches overlay/channel.
    Status refresh is at most 5Hz, sent only when visible state or enablement changes.
    At most one pending command Future is retained for local outcome feedback.
    """

    def __init__(self, playback, overlay, channel, stop_event, *, clock=monotonic):
        self.playback, self.overlay, self.channel = playback, overlay, channel
        self.stop_event = stop_event
        self.clock = clock
        self._stop = Event()
        self._lock = Lock()
        self._status = CapabilityStatus(False, "stopped")
        self._failure = None
        self._feedback = Outcome.OK
        self._pending = None
        self._hide_at = None
        self._sent = None
        self.thread = Thread(target=self._run, name="playback-controls", daemon=True)

    @property
    def status(self):
        with self._lock:
            return self._status

    @property
    def failure(self):
        with self._lock:
            return self._failure

    @property
    def feedback(self):
        with self._lock:
            return self._feedback

    def _report(self, reason, *, fatal=False):
        with self._lock:
            if self._failure is not None:
                return
            self._status = CapabilityStatus(reason == "ready", reason)
            if fatal:
                self._failure = "cleanup_failed"
                self.stop_event.set()

    def cancelled(self):
        return self._stop.is_set() or self.stop_event.is_set()

    def start(self):
        self.thread.start()

    def join(self):
        self._stop.set()
        self.thread.join(5.0)
        if self.thread.is_alive():
            self._report("cleanup_failed", fatal=True)
        if self.failure:
            raise RuntimeError("Controls cleanup_failed")

    def activity(self, value):
        action = Action(value)
        state = control_state(self.playback.status)
        if not state.enabled:
            return
        self._hide_at = self.clock() + AUTO_HIDE_SECONDS
        command = playback_command(action, state)
        outcome = Outcome.OK if action == Action.ACTIVITY else Outcome.UNAVAILABLE
        if command is not None:
            if self._pending is not None and not self._pending.done():
                outcome = Outcome.BUSY
            else:
                self._pending = self.playback.submit(*command)
                outcome = Outcome.OK
        with self._lock:
            self._feedback = outcome

    def refresh(self):
        state = control_state(self.playback.status)
        if not state.enabled:
            self._hide_at = None
        visible = self._hide_at is not None and self.clock() < self._hide_at
        state = replace(state, visible=visible)
        # Hidden capability changes need no pipe traffic and never reset activity.
        sent = state if visible else ControlState(enabled=state.enabled)
        if sent != self._sent:
            self.overlay.update(sent, timeout_seconds=0.5, cancelled=self.cancelled)
            self._sent = sent
        if self._pending is not None and self._pending.done():
            with self._lock:
                self._feedback = self._pending.result()
            self._pending = None

    def _run(self):
        try:
            self.overlay.start(timeout_seconds=2, cancelled=self.cancelled)
            self.channel.start()
            self._report("ready")
            while not self.cancelled():
                self.refresh()
                for source in (self.channel, self.overlay):
                    action = source.receive(
                        timeout_seconds=0.1, cancelled=self.cancelled
                    )
                    if action is not None:
                        self.activity(action)
        except CapabilityError as error:
            self._report(
                "cleanup_failed"
                if error.cleanup_failed or error.reason == "cleanup_failed"
                else "unavailable",
                fatal=error.cleanup_failed or error.reason == "cleanup_failed",
            )
        except Exception:
            self._report("unavailable")
        finally:
            for source in (self.overlay, self.channel):
                try:
                    source.stop()
                except Exception:
                    self._report("cleanup_failed", fatal=True)
            if self.cancelled() and self.failure is None:
                self._report("stopped")
