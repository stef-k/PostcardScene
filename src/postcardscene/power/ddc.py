"""One unambiguous DDC/CI display, with bounded D6 readback."""

import re

from ._backend import Backend
from ._types import Kind, PowerError, Reason, State

# Older brief output omits DRM fields; current ddcutil includes them. Only
# these bounded fields are accepted, and no EDID/connector data enters status.
_ENTRY = re.compile(
    rb"(?:Display ([1-9][0-9]{0,3})|Invalid display)\n"
    rb"[ \t]+I2C bus:[ \t]+/dev/i2c-([0-9]{1,4})\n"
    rb"(?:[ \t]+DRM connector:[ \t]+card[0-9]{1,4}-[A-Za-z0-9-]{1,64}\n)?"
    rb"(?:[ \t]+drm_connector_id:[ \t]+(?:-1|[0-9]{1,10})\n)?"
    rb"[ \t]+Monitor:[ \t]+[^\r\n]{1,512}(?:\n|$)"
)


class DdcBackend(Backend):
    def __init__(self, display: int | None = None):
        super().__init__(Kind.DDC)
        if display is not None and (
            type(display) is not int or not 1 <= display <= 9999
        ):
            raise ValueError("Invalid trusted DDC display selector.")
        self._display = display
        self._bus = None

    def _run(self, args, cancelled):
        return self._command.run(["--noconfig", "--brief", *args], {}, cancelled)

    def _operate(self, requested, cancelled):
        if self._bus is None:
            self._discover(cancelled)
        args = ["--bus", str(self._bus)]
        if requested is not None:
            self._run(
                [
                    *args,
                    "--noverify",
                    "setvcp",
                    "D6",
                    "0x01" if requested == State.ON else "0x04",
                ],
                cancelled,
            )
        return self._observe(args, cancelled)

    def _discover(self, cancelled):
        displays = _displays(self._run(["detect"], cancelled))
        if self._display is not None:
            displays = [entry for entry in displays if entry[0] == self._display]
        if not displays:
            raise PowerError(Reason.UNAVAILABLE)
        if len(displays) != 1:
            raise PowerError(Reason.AMBIGUOUS)
        bus = displays[0][1]
        if self._observe(["--bus", str(bus)], cancelled) == State.UNKNOWN:
            raise PowerError(Reason.UNKNOWN)
        # Keep the selected bus even when standby makes detection/query fail.
        # Replacing this capability/identity is the later coordinator's decision.
        self._bus = bus

    def _observe(self, args, cancelled):
        raw = self._run([*args, "getvcp", "D6"], cancelled)
        match = re.fullmatch(rb"VCP D6 SNC x([0-9a-fA-F]{2})\s*", raw)
        if match is None:
            raise PowerError(Reason.MALFORMED)
        # MCCS DPMS standby, suspend and off are all unambiguously non-on.
        return {1: State.ON, 2: State.OFF, 3: State.OFF, 4: State.OFF}.get(
            int(match[1], 16), State.UNKNOWN
        )


def _displays(raw: bytes) -> list[tuple[int, int]]:
    if not raw.strip() or raw.strip() == b"No displays found":
        return []
    blocks = _ENTRY.findall(raw)
    residue = _ENTRY.sub(b"", raw)
    if residue.strip() or not blocks or len(blocks) > 16:
        raise PowerError(Reason.MALFORMED)
    displays = [(int(number), int(bus)) for number, bus in blocks if number]
    if len({number for number, _ in displays}) != len(displays) or len(
        {bus for _, bus in displays}
    ) != len(displays):
        raise PowerError(Reason.AMBIGUOUS)
    return displays
