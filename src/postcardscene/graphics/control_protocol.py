"""Closed, target-free controls vocabulary shared with system Python (stdlib only)."""

import json
from dataclasses import asdict, dataclass
from enum import StrEnum

MAX_MESSAGE_BYTES = 1024


class Action(StrEnum):
    ACTIVITY = "activity"
    PREVIOUS = "previous"
    TOGGLE_PAUSE = "toggle_pause"
    NEXT = "next"
    SEEK_BACK = "seek_back_10"
    SEEK_FORWARD = "seek_forward_10"
    TOGGLE_MUTE = "toggle_mute"
    VOLUME_DOWN = "volume_down_10"
    VOLUME_UP = "volume_up_10"


@dataclass(frozen=True)
class ControlState:
    enabled: bool = False
    visible: bool = False
    paused: bool = False
    seekable: bool = False
    audio: bool = False
    muted: bool | None = None
    volume: int | None = None

    def __post_init__(self):
        if any(
            type(value) is not bool
            for value in (
                self.enabled,
                self.visible,
                self.paused,
                self.seekable,
                self.audio,
            )
        ):
            raise ValueError("invalid_state")
        if self.muted is not None and type(self.muted) is not bool:
            raise ValueError("invalid_state")
        if self.volume is not None and (
            type(self.volume) is not int or not 0 <= self.volume <= 100
        ):
            raise ValueError("invalid_state")
        if self.visible and not self.enabled:
            raise ValueError("invalid_state")

    def encode(self):
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=True)


def decode_state(line):
    try:
        if len(line) + 1 > MAX_MESSAGE_BYTES or not line.startswith(b"{"):
            raise ValueError
        # Reject duplicate keys rather than accepting an ambiguous schema.
        pairs = json.loads(line.decode("ascii"), object_pairs_hook=list)
        if not isinstance(pairs, list):
            raise ValueError
        data = dict(pairs)
        if len(data) != len(pairs) or set(data) != set(
            ControlState.__dataclass_fields__
        ):
            raise ValueError
        return ControlState(**data)
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("invalid_state") from None


def buttons(state):
    """Ordered visible labels/actions; no content text crosses this boundary."""
    result = [
        (Action.PREVIOUS, "Previous"),
        (Action.TOGGLE_PAUSE, "Play" if state.paused else "Pause"),
        (Action.NEXT, "Next"),
    ]
    if state.seekable:
        result.extend(((Action.SEEK_BACK, "-10s"), (Action.SEEK_FORWARD, "+10s")))
    if state.audio:
        result.extend(
            (
                (Action.TOGGLE_MUTE, "Unmute" if state.muted else "Mute"),
                (Action.VOLUME_DOWN, "Volume -10"),
                (Action.VOLUME_UP, "Volume +10"),
            )
        )
    return result
