"""Typed, target-free execution boundary for the future V0 presenter."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Protocol

from postcardscene.display_planner import DisplayStep


class Outcome(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    CANCELLED = "cancelled"
    CLEANUP_FAILED = "cleanup_failed"
    BUSY = "busy"
    REJECTED = "rejected"


class PresentationError(Exception):
    def __init__(self, outcome: Outcome):
        self.outcome = Outcome(outcome)
        super().__init__(self.outcome.value)


@dataclass(frozen=True)
class ContentSnapshot:
    active: bool = True
    ended: bool = False
    position_seconds: float | None = None
    duration_seconds: float | None = None
    seekable: bool = False
    audio_available: bool = False
    muted: bool | None = None
    volume: int | None = None


@dataclass(frozen=True)
class PlaybackStatus:
    state: str = "idle"
    reason: str = "idle"
    paused: bool = False
    output_suppressed: bool = False
    sequence_id: int | None = None
    membership_id: int | None = None
    scene_id: int | None = None
    widget_id: int | None = None
    kind: str | None = None
    remaining_seconds: float | None = None
    video: ContentSnapshot | None = None


class Presenter(Protocol):
    """All calls belong to one worker and must bound cancellation and cleanup.

    show re-resolves exact step authority and honors start_paused before video
    can become audible. clear retires content/audio to safe black; stop is an
    idempotent final retirement. Neither may report success with uncertain
    authority. Raise PresentationError(CLEANUP_FAILED) in that case. Ordinary
    failures use UNAVAILABLE; cancellation uses CANCELLED. No raw target or
    backend exception is part of this contract. #93 supplies the implementation.
    """

    def show(
        self, step: DisplayStep, *, start_paused: bool, cancelled: Callable[[], bool]
    ) -> ContentSnapshot: ...

    def snapshot(self, *, cancelled: Callable[[], bool]) -> ContentSnapshot: ...

    def pause(self, *, cancelled: Callable[[], bool]) -> ContentSnapshot: ...

    def resume(self, *, cancelled: Callable[[], bool]) -> ContentSnapshot: ...

    def seek_relative(
        self, seconds: float, *, cancelled: Callable[[], bool]
    ) -> ContentSnapshot: ...

    def mute(self, *, cancelled: Callable[[], bool]) -> ContentSnapshot: ...

    def unmute(self, *, cancelled: Callable[[], bool]) -> ContentSnapshot: ...

    def set_volume(
        self, volume: int, *, cancelled: Callable[[], bool]
    ) -> ContentSnapshot: ...

    def clear(self, *, cancelled: Callable[[], bool]) -> None: ...

    def stop(self) -> None: ...
