"""Private, bounded compositor-command channel for fixed playback actions."""

import socket
import stat
from pathlib import Path
from xml.etree import ElementTree

from . import WaylandSession
from ._capability import CapabilityError, CapabilityStatus, Deadline
from .control_protocol import Action

EVENTS = frozenset(action.value.encode("ascii") for action in Action)
SOCKET_NAME = "postcardscene-input.sock"


class InputChannel:
    def __init__(self, session):
        self.session = session
        self.path = session.runtime_directory / SOCKET_NAME
        self._socket = None
        self._identity = None
        self.status = CapabilityStatus(False, "stopped")

    def start(self):
        if self._socket is not None:
            return
        connection = None
        try:
            self.session.validate_directory()
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            connection.bind(str(self.path))  # Never unlink a stale/foreign owner.
            info = self.path.lstat()
            self._identity = (info.st_dev, info.st_ino)
            self.path.chmod(0o600)
            connection.setblocking(False)
            self._socket = connection
            self.status = CapabilityStatus(True, "ready")
        except (OSError, ValueError):
            if connection is not None:
                connection.close()
            self._remove_owned_path()
            self.status = CapabilityStatus(False, "input_unavailable")
            raise CapabilityError("input_unavailable") from None

    def receive(self, *, timeout_seconds=0.1, cancelled=None):
        import select

        deadline = Deadline(timeout_seconds, cancelled)
        if self._socket is None:
            raise CapabilityError("input_unavailable")
        try:
            while True:
                if select.select([self._socket], [], [], deadline.check())[0]:
                    data = self._socket.recv(33)
                    if data not in EVENTS:
                        raise CapabilityError("protocol_failed")
                    return data.decode("ascii")
        except CapabilityError as error:
            if error.reason == "timeout":
                return None
            raise
        except OSError:
            self.status = CapabilityStatus(False, "input_unavailable")
            raise CapabilityError("input_unavailable") from None

    def _remove_owned_path(self):
        if self._identity is not None:
            try:
                info = self.path.lstat()
                if (info.st_dev, info.st_ino) == self._identity:
                    self.path.unlink()
            except FileNotFoundError:
                pass
            self._identity = None

    def stop(self):
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        try:
            self._remove_owned_path()
        except OSError:
            raise CapabilityError("cleanup_failed") from None
        self.status = CapabilityStatus(False, "stopped")


def emit(session, event):
    """Fixed emitter destination under validated application runtime authority."""
    import os

    if not isinstance(event, str) or event not in Action._value2member_map_:
        raise CapabilityError("invalid_event")
    try:
        session.validate_directory()
        path = session.runtime_directory / SOCKET_NAME
        info = path.lstat()
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
            connection.settimeout(0.1)
            connection.sendto(event.encode("ascii"), str(path))
    except (OSError, ValueError):
        raise CapabilityError("input_unavailable") from None


def probe_keybinding(emitter: Path):
    """Test-only XML fragment; deliberately not installed in production rc.xml.

    A provisioned console script is the single executable. No shell metacharacters,
    expansion, arbitrary arguments, configured keys or runtime paths enter XML.
    """
    import re

    if (
        not isinstance(emitter, Path)
        or not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(emitter))
        or ".." in emitter.parts
    ):
        raise CapabilityError("invalid_spec")
    binding = ElementTree.Element("keybind", key="W-F12")
    action = ElementTree.SubElement(binding, "action", name="Execute")
    ElementTree.SubElement(action, "command").text = f"{emitter} activity"
    return ElementTree.tostring(binding, encoding="unicode")


def main():
    import sys

    if len(sys.argv) != 2:
        return 1
    try:
        emit(WaylandSession(), sys.argv[1])
    except CapabilityError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
