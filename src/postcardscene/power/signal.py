"""Signal-only power on the output already selected by graphics #63."""

import re

from postcardscene.graphics import WaylandSession
from postcardscene.graphics.output import DisplayStatus, select_connector
from postcardscene.graphics.output_probe import CONNECTOR, ProbeError, read_connectors

from ._backend import Backend
from ._types import Kind, PowerError, Reason, State


class SignalBackend(Backend):
    def __init__(
        self,
        session: WaylandSession,
        display: DisplayStatus | None = None,
        *,
        connector_override: str | None = None,
    ):
        super().__init__(Kind.SIGNAL)
        # Retain the selected identity across intentional off. Reconciliation
        # and replacement of this capability belong to the later single owner.
        self._output = (
            display.connector
            if display is not None
            and display.session_available
            and display.state == "ready"
            else None
        )
        if self._output is not None and not CONNECTOR.fullmatch(self._output):
            raise ValueError("Invalid selected Wayland output.")
        if connector_override is not None and (
            type(connector_override) is not str
            or not CONNECTOR.fullmatch(connector_override)
        ):
            raise ValueError("Invalid trusted display connector.")
        self._discover_output = display is None
        self._connector_override = connector_override
        self._session = session

    def _operate(self, requested, cancelled):
        if not self._session.inspect().available:
            raise PowerError(Reason.UNAVAILABLE)
        try:
            environment = self._session.client_environment()
        except (OSError, ValueError):
            raise PowerError(Reason.UNAVAILABLE) from None
        if self._output is None:
            self._select_output()
        if requested is not None:
            self._command.run(
                ["--on" if requested == State.ON else "--off", self._output],
                environment,
                cancelled,
            )
        raw = self._command.run([], environment, cancelled)
        return _observation(raw, self._output)

    def _select_output(self):
        if not self._discover_output:
            raise PowerError(Reason.NOT_CONFIGURED)
        try:
            connector = select_connector(read_connectors(), self._connector_override)
        except ProbeError as error:
            reason = (
                Reason.AMBIGUOUS if str(error) == "ambiguous" else Reason.UNAVAILABLE
            )
            raise PowerError(reason) from None
        if connector is None:
            raise PowerError(Reason.UNAVAILABLE)
        # #63 alone selects the connector. wlopm's named protocol evidence below
        # verifies its Wayland identity. Never reconcile modes to discover power:
        # that could turn an intentionally sleeping output back on.
        self._output = connector.name


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
