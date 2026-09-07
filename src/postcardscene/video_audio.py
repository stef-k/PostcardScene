"""Trusted audio launch policy and bounded private-IPC state normalization."""

from dataclasses import dataclass

from postcardscene.graphics._capability import CapabilityError
from postcardscene.video_selection import (
    VideoSelectionError,
    validate_video_configuration,
)


def intended_device(value):
    """One literal mpv device, never an option list or shell expression."""
    if value is None or value == "auto":
        return "auto"
    if (
        type(value) is not str
        or not 1 <= len(value) <= 256
        or not value.isascii()
        or any(not (char.isalnum() or char in "_./:,=-") for char in value)
        or value.startswith("-")
        or "/" not in value
    ):
        raise CapabilityError("invalid_audio_device")
    return value


@dataclass(frozen=True)
class AudioSnapshot:
    enabled: bool = False
    available: bool = False
    muted: bool | None = None
    volume: float | None = None
    reason: str = "disabled"
    device_policy: str = "auto"


@dataclass(frozen=True)
class AudioPolicy:
    enabled: bool = False
    volume: int = 50

    @classmethod
    def from_configuration(cls, configuration):
        try:
            values = validate_video_configuration("video", configuration)
        except VideoSelectionError:
            raise CapabilityError("invalid_audio_policy") from None
        return cls(values["audio_enabled"], values["volume"])

    def arguments(self, device):
        if not self.enabled:
            return ("--audio=no",)
        return (
            "--audio=auto",
            f"--volume={self.volume}",
            "--volume-max=100",
            "--mute=no",
            f"--audio-device={device}",
            "--audio-fallback-to-null=yes",
            "--audio-spdif=",
            "--replaygain=no",
        )

    def snapshot(self, device, *, reason=None, request=None, deadline=None):
        """Read only scalar properties; the controller owns requests and deadlines."""
        policy = "auto" if device == "auto" else "explicit"
        reason = "disabled" if not self.enabled else reason
        if reason is not None:
            return AudioSnapshot(self.enabled, reason=reason, device_policy=policy)
        values = {
            name: request(["get_property", name], deadline, property_read=True)
            for name in ("current-tracks/audio/id", "current-ao", "mute", "volume")
        }
        track, output = values["current-tracks/audio/id"], values["current-ao"]
        if track is not None and (type(track) is not int or track < 0):
            raise CapabilityError("protocol_failed")
        if output is not None and (type(output) is not str or not output):
            raise CapabilityError("protocol_failed")
        if output == "null":
            reason = "device_unavailable"
        elif track is None:
            reason = "no_track"
        elif output is None:
            reason = "device_unavailable"
        else:
            reason = "ready"
        if reason != "ready":
            return AudioSnapshot(self.enabled, reason=reason, device_policy=policy)
        muted, volume = values["mute"], values["volume"]
        if (
            type(muted) is not bool
            or type(volume) not in (int, float)
            or not 0 <= volume <= 100
        ):
            raise CapabilityError("protocol_failed")
        return AudioSnapshot(True, True, muted, float(volume), reason, policy)
