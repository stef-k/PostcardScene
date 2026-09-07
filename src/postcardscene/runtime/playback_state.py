"""Serialized playback execution; order, selection and history stay in #88."""

from dataclasses import replace
from math import isfinite

from sqlalchemy.exc import SQLAlchemyError

from postcardscene.display_planner import PlanStatus
from postcardscene.persistence import DatabaseError
from postcardscene.playback_configuration import Eligibility
from postcardscene.runtime.presentation import (
    ContentSnapshot,
    Outcome,
    PlaybackStatus,
    PresentationError,
)

FAILURE_LIMIT = 8
BACKOFF_SECONDS = 5.0
VIDEO_POLL_SECONDS = 0.5


def dwell_seconds(configuration, kind):
    duration = configuration.membership.duration_override_seconds
    if duration is None:
        duration = configuration.scene.duration_seconds
    if duration is None and kind != "video":
        duration = configuration.settings.default_scene_dwell_seconds
    return duration


def valid_command(name, value):
    if name in {
        "next",
        "previous",
        "pause",
        "resume",
        "toggle_pause",
        "mute",
        "unmute",
    }:
        return value is None
    if name == "set_output_suppressed":
        return type(value) is bool
    if name == "set_volume":
        return type(value) is int and 0 <= value <= 100
    if name == "seek_relative":
        return type(value) in (int, float) and isfinite(value) and abs(value) <= 3600
    return False


class PlaybackState:
    """Worker-private state. Tests may drive this same seam with a fake clock."""

    def __init__(self, planner, configuration, presenter, clock, publish):
        self.planner = planner
        self.configuration = configuration
        self.presenter = presenter
        self.clock = clock
        self.publish = publish
        self.current = None
        self.content = None
        self.paused = False
        self.suppressed = False
        self.remaining = None
        self.updated = clock()
        self.poll_at = 0.0
        self.retry_at = 0.0
        self.failures = 0
        self.state = "idle"
        self.reason = "idle"

    def report(self, state=None, reason=None):
        if state is not None:
            self.state = state
        if reason is not None:
            self.reason = reason
        step = self.current
        self.publish(
            PlaybackStatus(
                state=self.state,
                reason=self.reason,
                paused=self.paused,
                sequence_id=step.sequence_id if step else None,
                membership_id=step.membership_id if step else None,
                scene_id=step.scene_id if step else None,
                widget_id=step.widget_id if step else None,
                kind=step.kind if step else None,
                remaining_seconds=self.remaining,
                video=self.content if step and step.kind == "video" else None,
            )
        )

    def account_time(self):
        now = self.clock()
        if self.content is not None and not self.paused and not self.suppressed:
            if self.remaining is not None:
                self.remaining = max(0.0, self.remaining - (now - self.updated))
        self.updated = now

    def clear(self):
        # Retirement must finish even when a newer command cancelled show.
        # The presenter owns a bounded cleanup deadline independent of that token.
        try:
            self.presenter.clear(cancelled=lambda: False)
        except Exception:
            raise PresentationError(Outcome.CLEANUP_FAILED) from None
        self.content = None

    def idle(self, *, intentional=False):
        self.clear()
        self.current = None
        self.remaining = None
        self.retry_at = self.clock() + BACKOFF_SECONDS
        self.report(
            "idle" if intentional else "degraded",
            "idle" if intentional else "unavailable",
        )

    def failed(self, error):
        if error.outcome == Outcome.CLEANUP_FAILED:
            raise error
        self.clear()
        if error.outcome == Outcome.CANCELLED:
            # Keep the current identity for a state command's re-presentation.
            self.report("idle", "cancelled")
            return
        self.current = None
        self.remaining = None
        self.failures += 1
        self.report("degraded", "unavailable")
        if self.failures >= FAILURE_LIMIT:
            self.retry_at = self.clock() + BACKOFF_SECONDS
            self.failures = 0

    def advance(self, direction, cancelled):
        result = getattr(self.planner, direction)()
        if result.status == PlanStatus.HISTORY_BOUNDARY:
            return Outcome.UNAVAILABLE
        if cancelled():
            raise PresentationError(Outcome.CANCELLED)
        if result.status != PlanStatus.READY:
            config = self.configuration()
            if config.eligibility != Eligibility.READY:
                self.idle(intentional=config.eligibility == Eligibility.IDLE)
                return Outcome.UNAVAILABLE
            raise PresentationError(Outcome.UNAVAILABLE)
        self.clear()
        self.current = result.step
        self.remaining = None
        self.retry_at = 0.0
        if self.suppressed:
            self.report("idle", "suppressed")
        else:
            self.present(cancelled)
        return Outcome.OK

    def present(self, cancelled):
        result = self.planner.revalidate(self.current)
        if result.status != PlanStatus.READY:
            raise PresentationError(Outcome.UNAVAILABLE)
        config = self.configuration(membership_id=self.current.membership_id)
        if (
            config.eligibility != Eligibility.READY
            or config.sequence.id != self.current.sequence_id
            or config.scene.id != self.current.scene_id
            or config.scene.widget_id != self.current.widget_id
        ):
            raise PresentationError(Outcome.UNAVAILABLE)
        self.report("presenting", "loading")
        content = self.presenter.show(
            self.current, start_paused=self.paused, cancelled=cancelled
        )
        if cancelled():
            raise PresentationError(Outcome.CANCELLED)
        self.accept_content(content)
        if not self.content.active or self.content.ended:
            raise PresentationError(Outcome.UNAVAILABLE)
        self.remaining = dwell_seconds(config, self.current.kind)
        self.updated = self.clock()
        self.poll_at = self.updated + VIDEO_POLL_SECONDS
        self.failures = 0
        self.report("paused" if self.paused else "playing", "ready")

    def accept_content(self, content):
        if not isinstance(content, ContentSnapshot):
            raise PresentationError(Outcome.UNAVAILABLE)
        if not content.active and not content.ended:
            raise PresentationError(Outcome.UNAVAILABLE)

        # Only bounded scalars cross into public status, even from a faulty adapter.
        def seconds(value):
            return (
                value
                if type(value) in (int, float) and isfinite(value) and value >= 0
                else None
            )

        self.content = replace(
            content,
            active=content.active is True,
            ended=content.ended is True,
            position_seconds=seconds(content.position_seconds),
            duration_seconds=seconds(content.duration_seconds),
            seekable=content.seekable is True,
            audio_available=content.audio_available is True,
            muted=content.muted if type(content.muted) is bool else None,
            volume=content.volume
            if type(content.volume) is int and 0 <= content.volume <= 100
            else None,
        )

    def tick(self, cancelled):
        self.account_time()
        if self.suppressed:
            return None
        if self.retry_at > self.clock():
            return self.retry_at - self.clock()
        if self.content is None:
            if self.current is None:
                self.advance("next", cancelled)
            else:
                self.present(cancelled)
            return 0.0
        video = self.current.kind == "video"
        if video and self.clock() >= self.poll_at:
            self.accept_content(self.presenter.snapshot(cancelled=cancelled))
            self.account_time()
            self.poll_at = self.clock() + VIDEO_POLL_SECONDS
        if not self.paused and (self.remaining == 0 or (video and self.content.ended)):
            self.advance("next", cancelled)
            return 0.0
        self.report()
        wait = None if self.paused else self.remaining
        if video:
            poll = max(0.0, self.poll_at - self.clock())
            wait = poll if wait is None else min(wait, poll)
        return wait

    def command(self, name, value, cancelled):
        self.account_time()
        if name in {"next", "previous"}:
            return self.advance(name, cancelled)
        if name == "set_output_suppressed":
            if value == self.suppressed:
                return Outcome.OK
            self.clear()
            self.suppressed = value
            self.retry_at = 0.0
            self.report("idle", "suppressed" if value else "idle")
            return Outcome.OK
        if name in {"pause", "resume", "toggle_pause"}:
            paused = not self.paused if name == "toggle_pause" else name == "pause"
            if paused != self.paused:
                self.paused = paused
                if self.content and self.current.kind == "video":
                    operation = (
                        self.presenter.pause if paused else self.presenter.resume
                    )
                    self.accept_content(operation(cancelled=cancelled))
                self.updated = self.clock()
            if self.content:
                self.report("paused" if self.paused else "playing", "ready")
            else:
                self.report()
            return Outcome.OK
        return self.video_command(name, value, cancelled)

    def video_command(self, name, value, cancelled):
        if not self.content or self.current.kind != "video" or not self.content.active:
            return Outcome.UNAVAILABLE
        if name == "seek_relative":
            available = self.content.seekable
        else:
            available = self.content.audio_available
        if not available:
            return Outcome.UNAVAILABLE
        operation = getattr(self.presenter, name)
        try:
            content = (
                operation(cancelled=cancelled)
                if value is None
                else operation(value, cancelled=cancelled)
            )
        except PresentationError as error:
            if error.outcome == Outcome.UNAVAILABLE:
                return Outcome.UNAVAILABLE
            raise
        self.accept_content(content)
        self.report()
        return Outcome.OK

    def execute(self, operation, cancelled):
        """Contain known content/storage failures; unexpected bugs stay fatal."""
        try:
            return operation(cancelled)
        except (DatabaseError, SQLAlchemyError):
            self.failed(PresentationError(Outcome.UNAVAILABLE))
        except PresentationError as error:
            self.failed(error)
            return error.outcome
        return Outcome.UNAVAILABLE
