"""Trusted launcher/profile authority and a pinned Linux process group."""

import fcntl
import os
import signal
import stat
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from ._errors import ChromiumError, Failure

BLACK_PAGE = "data:text/html,%3Chtml%20style=%22background:black%22%3E%3C/html%3E"
ENVIRONMENT_KEYS = frozenset(
    {"PATH", "LANG", "LC_ALL", "SNAP_NAME", "SNAP_INSTANCE_NAME"}
)


class BrowserContext(StrEnum):
    TRUSTED_IMAGE = "trusted_image"
    UNTRUSTED_WEB = "untrusted_web"


@dataclass(frozen=True, repr=False)
class ChromiumLaunchSpec:
    """Host-only package invocation; never construct from content configuration.

    Prefix arguments are package invocation words, not Chromium options. The
    executable must remain in the owned group (exec or wait, never daemonize).
    Environment values are non-secret provisioning data, not inherited state.
    """

    command: tuple[str, ...]
    profile_root: Path
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        try:
            command = tuple(self.command)
            root = Path(self.profile_root)
            overlay = dict(self.environment)
            valid = (
                not isinstance(self.command, str)
                and 1 <= len(command) <= 16
                and all(
                    isinstance(arg, str) and 0 < len(arg) <= 4096 and "\0" not in arg
                    for arg in command
                )
                and Path(command[0]).is_absolute()
                and all(not arg.startswith("-") for arg in command[1:])
                and root.is_absolute()
                and ".." not in root.parts
                and "\0" not in str(root)
                and overlay.keys() <= ENVIRONMENT_KEYS
                and all(
                    isinstance(value, str) and len(value) <= 4096 and "\0" not in value
                    for value in overlay.values()
                )
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ChromiumError(Failure.INVALID_SPEC)
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "profile_root", root)
        object.__setattr__(self, "environment", MappingProxyType(overlay))


def private_directory(path: Path) -> None:
    info = path.lstat()
    if (
        os.geteuid() == 0
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or path.resolve() != path
    ):
        raise ChromiumError(Failure.INVALID_SPEC)


def owned_file(path: Path, limit: int) -> bytes:
    """No symlinks, devices, blocking FIFO reads or foreign startup metadata."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
            or info.st_nlink != 1
            or info.st_size > limit
        ):
            raise ChromiumError(Failure.INVALID_SPEC)
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise ChromiumError(Failure.INVALID_SPEC)
        return data
    finally:
        os.close(fd)


class Profile:
    """A context marker prevents reusing roots across trust classes, even later.

    flock serializes controllers across runtime processes. Ancestors are trusted
    local host directories; provisioning must not point this at network storage.
    No existing personal directory is adopted or recursively changed/deleted.
    """

    def __init__(self, root: Path, context: BrowserContext):
        self.root = root
        self.path = root / "profile"
        self.metadata = self.path / "DevToolsActivePort"
        self._lock = None
        marker = root / ".postcardscene-chromium"
        try:
            private_directory(root)
            if not marker.exists():
                if any(root.iterdir()):
                    raise ChromiumError(Failure.INVALID_SPEC)
                fd = os.open(
                    marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(context.value.encode("ascii"))
            if owned_file(marker, 32) != context.value.encode("ascii"):
                raise ChromiumError(Failure.INVALID_SPEC)
            self.path.mkdir(mode=0o700, exist_ok=True)
            private_directory(self.path)
        except (OSError, ValueError):
            raise ChromiumError(Failure.INVALID_SPEC) from None

    def acquire(self):
        try:
            private_directory(self.root)
            private_directory(self.path)
            marker = self.root / ".postcardscene-chromium"
            owned_file(marker, 32)
            self._lock = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.clear_metadata()
        except (OSError, ChromiumError):
            self.release()
            raise ChromiumError(Failure.INVALID_SPEC) from None

    def clear_metadata(self):
        try:
            owned_file(self.metadata, 512)
            self.metadata.unlink()
        except FileNotFoundError:
            pass

    def release(self):
        if self._lock is not None:
            os.close(self._lock)
            self._lock = None


def launch_arguments(spec: ChromiumLaunchSpec, profile: Profile) -> list[str]:
    return [
        *spec.command,
        "--ozone-platform=wayland",
        f"--user-data-dir={profile.path}",
        "--kiosk",
        "--no-first-run",
        "--no-default-browser-check",
        "--noerrdialogs",
        "--disable-session-crashed-bubble",
        "--disable-features=ChromeWhatsNewUI",
        "--disable-extensions",
        "--disable-plugins",
        "--disable-sync",
        "--disable-background-networking",
        "--disable-background-mode",
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=0",
        BLACK_PAGE,
    ]


class OwnedProcess:
    """Retain the unreaped leader until the last group signal to prevent PID reuse.

    Only this object may wait/reap its child. A crashed wrapper's zombie leader
    pins the group ID while cleanup terminates its still-running descendants.
    Launcher children must not escape with setsid; systemd is outer supervision.
    """

    def __init__(self, argv: list[str], environment: dict[str, str]):
        try:
            self._child = subprocess.Popen(
                argv,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                start_new_session=True,
                close_fds=True,
            )
        except OSError:
            raise ChromiumError(Failure.STARTUP_FAILED) from None
        self.pid = self._child.pid
        self._reaped = False

    def exited(self) -> bool:
        if self._reaped:
            return True
        try:
            return (
                os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                is not None
            )
        except ChildProcessError:
            # Another reaper invalidated our authority. Never signal this PID.
            self._reaped = True
            raise ChromiumError(Failure.CLEANUP_FAILED) from None

    def stop(self):
        if self._reaped:
            return
        try:
            self.exited()  # Verify that we still own the unreaped leader.
            os.killpg(self.pid, signal.SIGTERM)
            time.sleep(0.25)
            os.killpg(self.pid, signal.SIGKILL)
            self._child.wait(timeout=0.5)
            self._reaped = True
        except (OSError, subprocess.TimeoutExpired):
            raise ChromiumError(Failure.CLEANUP_FAILED) from None
