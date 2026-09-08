"""Signal-only power on the output already selected by graphics #63."""

import re

from postcardscene.graphics import WaylandSession
from postcardscene.graphics.output import DisplayStatus
from postcardscene.graphics.output_probe import CONNECTOR

from ._backend import Backend
from ._types import Kind, PowerError, Reason, State


class SignalBackend(Backend):
    def __init__(self, session: WaylandSession, display: DisplayStatus):
        super().__init__(Kind.SIGNAL)
        # Retain the selected identity across intentional off. Reconciliation
        # and replacement of this capability belong to the later single owner.
        self._output = (
            display.connector
            if display.session_available and display.state == "ready"
            else None
        )
        if self._output is not None and not CONNECTOR.fullmatch(self._output):
            raise ValueError("Invalid selected Wayland output.")
        self._session = session

    def _operate(self, requested, cancelled):
        if self._output is None:
            raise PowerError(Reason.NOT_CONFIGURED)
        if not self._session.inspect().available:
            raise PowerError(Reason.UNAVAILABLE)
        try:
            environment = self._session.client_environment()
        except (OSError, ValueError):
            raise PowerError(Reason.UNAVAILABLE) from None
        if requested is not None:
            self._command.run(
                ["--on" if requested == State.ON else "--off", self._output],
                environment,
                cancelled,
            )
        raw = self._command.run([], environment, cancelled)
        return _observation(raw, self._output)


def _observation(raw: bytes, output: str) -> State:
    """Match the printed query to an actual mode event for this output only.

    wlopm's query branch can print calloc's off value after a protocol failed
    event. WAYLAND_DEBUG evidence is private, capped with stdout and discarded.
    Unknown trace formats or hotplug during the query fail closed.
    """
    lines = raw.splitlines()
    printed = []
    trace = []
    for line in lines:
        event = re.fullmatch(
            rb"\[[ \t]*[0-9]+\.[0-9]+\] (?:\{[^}\r\n]{1,64}\} )?(.{1,2048})",
            line,
        )
        if event:
            trace.append(event[1].strip())
        else:
            printed.append(line)
    modes = _printed_modes(printed)
    target = output.encode("ascii")
    if target not in modes:
        raise PowerError(Reason.UNAVAILABLE)
    events = b"\n".join(trace)
    names = re.findall(
        rb'(?m)^wl_output[@#]([0-9]+)\.name\("' + target + rb'"\)$', events
    )
    if len(names) != 1 or b".global_remove(" in events:
        raise PowerError(Reason.UNKNOWN)
    objects = re.findall(
        rb"(?m)^-> zwlr_output_power_manager_v1[@#][0-9]+\.get_output_power\(new id zwlr_output_power_v1[@#]([0-9]+), wl_output[@#]"
        + names[0]
        + rb"\)$",
        events,
    )
    if len(objects) != 1:
        raise PowerError(Reason.UNKNOWN)
    prefix = rb"(?m)^zwlr_output_power_v1[@#]" + objects[0]
    if re.search(prefix + rb"\.failed\(\)$", events):
        raise PowerError(Reason.UNAVAILABLE)
    values = re.findall(prefix + rb"\.mode\(([0-9]+)\)$", events)
    if not values or any(value not in (b"0", b"1") for value in values):
        raise PowerError(Reason.UNKNOWN)
    observed = State.ON if values[-1] == b"1" else State.OFF
    if observed != modes[target]:
        raise PowerError(Reason.MALFORMED)
    return observed


def _printed_modes(lines: list[bytes]) -> dict[bytes, State]:
    if not lines or len(lines) > 64:
        raise PowerError(Reason.MALFORMED)
    modes = {}
    for line in lines:
        match = re.fullmatch(rb"([A-Za-z0-9_-]{1,128}) (on|off)", line)
        if match is None or match[1] in modes:
            raise PowerError(Reason.MALFORMED)
        modes[match[1]] = State(match[2].decode("ascii"))
    return modes
