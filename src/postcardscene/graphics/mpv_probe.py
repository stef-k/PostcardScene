"""Inert native-Wayland window proof, deliberately not a media controller."""

import re
from threading import Event, Thread

from ._capability import (
    CapabilityError,
    CapabilityStatus,
    Deadline,
    HelperProcess,
    client_environment,
    command_words,
)

PROBE_FLAGS = (
    "--no-config",
    "--gpu-context=wayland",
    "--vo=gpu",
    "--force-window=immediate",
    "--idle=yes",
    "--fullscreen",
    "--border=no",
    "--input-default-bindings=no",
    "--input-vo-keyboard=no",
    "--osc=no",
    "--audio=no",
    "--load-scripts=no",
    "--terminal=no",
)


class SurfaceCommit:
    """Observe a configured, buffer-backed xdg surface, never window titles.

    WAYLAND_DEBUG is probe-only evidence. Lines are capped and discarded; it
    never becomes a public log. Unknown trace formats fail closed at timeout.
    """

    def __init__(self):
        self.surface = None
        self.xdg = None
        self.configured = False
        self.attached = False

    def feed(self, line):
        match = re.search(
            rb"get_xdg_surface\(new id xdg_surface[@#](\d+), wl_surface[@#](\d+)\)",
            line,
        )
        if match:
            self.xdg, self.surface = match.groups()
        if self.surface is None:
            return False
        if re.search(rb"xdg_surface[@#]" + self.xdg + rb"\.ack_configure\(", line):
            self.configured = True
        if re.search(
            rb"wl_surface[@#]" + self.surface + rb"\.attach\(wl_buffer[@#]", line
        ):
            self.attached = True
        return bool(
            self.configured
            and self.attached
            and re.search(rb"wl_surface[@#]" + self.surface + rb"\.commit\(", line)
        )


class MpvSurfaceProbe:
    def __init__(self, session, command):
        self.session = session
        self.command = command_words(command)
        self._process = None
        self._thread = None
        self._halt = Event()
        self._ready = Event()
        self._reason = "stopped"

    @property
    def status(self):
        if self._process is not None and self._process.exited():
            self._reason = "helper_exited"
        return CapabilityStatus(self._reason == "ready", self._reason)

    def ensure_started(self, *, timeout_seconds=10, cancelled=None):
        deadline = Deadline(timeout_seconds, cancelled)
        try:
            deadline.check()
            environment = client_environment(self.session)
            if self._process is not None:
                if self.status.available:
                    return
                raise CapabilityError(self._reason)
            self._halt.clear()
            self._ready.clear()
            self._reason = "starting"
            self._process = HelperProcess(
                [*self.command, *PROBE_FLAGS],
                {**environment, "WAYLAND_DEBUG": "1"},
                trace=True,
            )
            self._thread = Thread(target=self._observe, daemon=True)
            self._thread.start()
            while not self._ready.wait(deadline.check()):
                if self._reason != "starting":
                    raise CapabilityError(self._reason)
            if not self.status.available:
                raise CapabilityError(self._reason)
        except CapabilityError as error:
            try:
                self.stop()
            except CapabilityError:
                error.cleanup_failed = True
            self._reason = error.reason
            raise

    def _observe(self):
        commit = SurfaceCommit()
        while not self._halt.is_set():
            try:
                line = self._process.line(Deadline(0.2))
                if commit.feed(line):
                    self._reason = "ready"
                    self._ready.set()
            except CapabilityError as error:
                if error.reason == "timeout":
                    continue
                self._reason = error.reason
                return

    def stop(self):
        self._halt.set()
        if self._thread is not None:
            self._thread.join(0.4)
            if self._thread.is_alive():
                raise CapabilityError("cleanup_failed")
            self._thread = None
        if self._process is not None:
            self._process.stop()
            self._process = None
        self._reason = "stopped"
