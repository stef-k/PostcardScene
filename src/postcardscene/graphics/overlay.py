"""Application-side overlay protocol; imports no GI or toolkit modules."""

from collections import deque
from pathlib import Path

from ._capability import (
    CapabilityError,
    CapabilityStatus,
    Deadline,
    HelperProcess,
    client_environment,
    command_words,
)


class Overlay:
    def __init__(self, session, system_python):
        self.session = session
        self.command = command_words((system_python,))
        self._process = None
        self._reason = "stopped"
        self.visible = False
        self._events = deque(maxlen=16)

    @property
    def status(self):
        if self._process is not None and self._process.exited():
            self._reason = "helper_exited"
            self.visible = False
        return CapabilityStatus(self._reason == "ready", self._reason)

    def start(self, *, timeout_seconds=5, cancelled=None):
        deadline = Deadline(timeout_seconds, cancelled)
        try:
            deadline.check()
            environment = client_environment(self.session)
            if self._process is not None:
                if self.status.available:
                    return
                raise CapabilityError(self._reason)
            self._process = HelperProcess(
                [*self.command, str(Path(__file__).with_name("overlay_helper.py"))],
                environment,
            )
            self._expect(b"ready", deadline)
            self._reason = "ready"
        except CapabilityError as error:
            self._fail(error)

    def _expect(self, expected, deadline):
        while True:
            line = self._process.line(deadline)
            if line == expected:
                return
            if line in (b"activity", b"probe_action"):
                self._events.append(line.decode("ascii"))
            else:
                raise CapabilityError("protocol_failed")

    def _command(self, command, reply, *, timeout_seconds=2, cancelled=None):
        deadline = Deadline(timeout_seconds, cancelled)
        try:
            if not self.status.available:
                raise CapabilityError("unavailable")
            self._process.send(command, deadline)
            self._expect(reply, deadline)
        except CapabilityError as error:
            self._fail(error)

    def show(self, **kwargs):
        self._command("show", b"shown", **kwargs)
        self.visible = True

    def hide(self, **kwargs):
        self._command("hide", b"hidden", **kwargs)
        self.visible = False

    def receive(self, *, timeout_seconds=0.1, cancelled=None):
        if self._events:
            return self._events.popleft()
        try:
            if not self.status.available:
                raise CapabilityError("unavailable")
            line = self._process.line(Deadline(timeout_seconds, cancelled))
            if line not in (b"activity", b"probe_action"):
                raise CapabilityError("protocol_failed")
            return line.decode("ascii")
        except CapabilityError as error:
            if error.reason == "timeout":
                return None
            self._fail(error)

    def stop(self):
        if self._process is not None:
            # Ask the helper to unmap/exit; TERM/KILL bounds a hung peer.
            try:
                self._process.send("stop", Deadline(0.1))
            except CapabilityError:
                pass
            self._process.stop()
            self._process = None
        self.visible = False
        self._events.clear()
        self._reason = "stopped"

    def _fail(self, error):
        try:
            self.stop()
        except CapabilityError:
            error.cleanup_failed = True
        self._reason = error.reason
        raise error from None
