"""Linux appliance-session capability, independent of Flask and content policy."""

import os
import socket
import stat
import struct
import time
from dataclasses import dataclass
from pathlib import Path

RUNTIME_DIRECTORY = Path("/run/postcardscene-wayland")
WAYLAND_DISPLAY = "wayland-0"


@dataclass(frozen=True)
class SessionStatus:
    available: bool
    compositor_state: str
    runtime_directory_valid: bool
    reason: str

    def public_diagnostics(self) -> dict[str, str | bool]:
        """Only fixed vocabulary; never serialize the process environment."""
        return {
            "backend": "wayland/labwc",
            "available": self.available,
            "compositor_state": self.compositor_state,
            "runtime_directory_valid": self.runtime_directory_valid,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class WaylandSession:
    """One private local runtime directory, provisioned by systemd.

    The caller runs as the non-root graphical user. Ancestors are trusted host
    authority. A probe is a point-in-time capability check, not output/panel health.
    """

    runtime_directory: Path = RUNTIME_DIRECTORY

    def __post_init__(self):
        path = Path(self.runtime_directory)
        if not path.is_absolute() or ".." in path.parts or "\0" in str(path):
            raise ValueError("An absolute local runtime directory is required.")
        if len(os.fsencode(path / WAYLAND_DISPLAY)) >= 108:
            raise ValueError("Wayland socket path is too long.")
        object.__setattr__(self, "runtime_directory", path)

    @property
    def socket_path(self) -> Path:
        return self.runtime_directory / WAYLAND_DISPLAY

    def validate_directory(self) -> None:
        info = self.runtime_directory.lstat()
        if (
            os.geteuid() == 0
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or self.runtime_directory.resolve() != self.runtime_directory
        ):
            raise ValueError(
                "Runtime directory requires non-root ownership and mode 0700."
            )

    def client_environment(self) -> dict[str, str]:
        """Explicit graphics variables to compose with a client's own allowlist.

        Consumers must omit DISPLAY/WAYLAND_SOCKET and select native Wayland in
        their engine arguments; this mapping is not the parent's full environment.
        """
        self.validate_directory()
        return {
            "XDG_RUNTIME_DIR": str(self.runtime_directory),
            "WAYLAND_DISPLAY": WAYLAND_DISPLAY,
            "XDG_SESSION_TYPE": "wayland",
            "XDG_CURRENT_DESKTOP": "labwc",
            "GDK_BACKEND": "wayland",
            "QT_QPA_PLATFORM": "wayland",
        }

    def inspect(self, *, compositor_pid: int | None = None) -> SessionStatus:
        """Bounded responsiveness and peer identity check (at most 250 ms I/O).

        Supply systemd's MainPID when available to reject a different compositor.
        No PID file, cached ready flag, environment import or distro branch exists.
        """
        if compositor_pid is not None and (
            type(compositor_pid) is not int or compositor_pid <= 0
        ):
            raise ValueError("Compositor PID must be a positive integer.")
        try:
            self.validate_directory()
        except (OSError, ValueError):
            return SessionStatus(False, "unknown", False, "invalid_runtime_directory")
        if compositor_pid is not None:
            try:
                os.kill(compositor_pid, 0)
            except ProcessLookupError:
                return SessionStatus(False, "stopped", True, "compositor_exited")
            except PermissionError:
                return SessionStatus(False, "unknown", True, "compositor_inaccessible")
        try:
            info = self.socket_path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                return SessionStatus(False, "unknown", True, "invalid_socket")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                deadline = time.monotonic() + 0.25
                connection.settimeout(0.25)
                connection.connect(str(self.socket_path))
                pid, uid, _ = struct.unpack(
                    "3i",
                    connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12),
                )
                if uid != os.geteuid() or (
                    compositor_pid is not None and pid != compositor_pid
                ):
                    return SessionStatus(False, "unknown", True, "unexpected_peer")
                _sync(connection, deadline)
        except (OSError, ValueError):
            return SessionStatus(False, "unavailable", True, "wayland_unresponsive")
        return SessionStatus(True, "running", True, "ready")


def _sync(connection: socket.socket, deadline: float) -> None:
    """Core wl_display.sync only: no registry, output or renderer protocol.

    Native-endian uint32 wire messages: display object 1, sync opcode 0,
    new callback object 2. Its first event must be callback.done (opcode 0).
    See the Wayland core protocol and wire-format specification.
    """
    connection.sendall(struct.pack("=III", 1, 12 << 16, 2))
    reply = bytearray()
    while len(reply) < 12:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        connection.settimeout(remaining)
        chunk = connection.recv(12 - len(reply))
        if not chunk:
            raise ValueError("Wayland closed before sync completion.")
        reply.extend(chunk)
    object_id, header, _ = struct.unpack("=III", reply)
    if object_id != 2 or header != 12 << 16:
        raise ValueError("Wayland did not complete sync.")
