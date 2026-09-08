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
        lines = raw.splitlines()
        if not lines or len(lines) > 64:
            raise PowerError(Reason.MALFORMED)
        modes = {}
        for line in lines:
            match = re.fullmatch(rb"([A-Za-z0-9_-]{1,128}) (on|off)", line)
            if match is None or match[1] in modes:
                raise PowerError(Reason.MALFORMED)
            modes[match[1]] = State(match[2].decode("ascii"))
        if self._output.encode("ascii") not in modes:
            raise PowerError(Reason.UNAVAILABLE)
        return modes[self._output.encode("ascii")]
