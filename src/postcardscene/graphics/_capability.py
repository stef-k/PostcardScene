"""Bounded helper plumbing shared only by the #65 graphical probes."""

import math
import os
import select
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


class CapabilityError(Exception):
    """Fixed public reason; raw helper output is never exception text."""

    def __init__(self, reason):
        self.reason = reason
        self.cleanup_failed = False
        super().__init__(reason)


@dataclass(frozen=True)
class CapabilityStatus:
    available: bool
    reason: str

    def public_diagnostics(self):
        return {"available": self.available, "reason": self.reason}


def command_words(command):
    try:
        words = tuple(command)
        valid = (
            not isinstance(command, str)
            and 1 <= len(words) <= 16
            and all(
                isinstance(word, str) and 0 < len(word) <= 4096 and "\0" not in word
                for word in words
            )
            and Path(words[0]).is_absolute()
            and all(not word.startswith("-") for word in words[1:])
        )
    except TypeError:
        valid = False
    if not valid:
        raise CapabilityError("invalid_spec")
    return words


class Deadline:
    def __init__(self, seconds, cancelled=None):
        if (
            not isinstance(seconds, (float, int))
            or not math.isfinite(seconds)
            or not 0 < seconds <= 60
        ):
            raise CapabilityError("invalid_spec")
        self.end = time.monotonic() + seconds
        self.cancelled = cancelled

    def check(self):
        if self.cancelled and self.cancelled():
            raise CapabilityError("cancelled")
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise CapabilityError("timeout")
        return min(remaining, 0.05)


class HelperProcess:
    """Pinned group authority plus nonblocking, capped line protocol.

    Only this object may reap the leader. Host launchers must exec/wait and keep
    descendants in this group. Systemd remains the outer owner-death boundary.
    """

    def __init__(self, argv, environment, *, trace=False):
        try:
            self.child = subprocess.Popen(
                argv,
                env=environment,
                shell=False,
                start_new_session=True,
                close_fds=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL if trace else subprocess.PIPE,
                stderr=subprocess.PIPE if trace else subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError:
            raise CapabilityError("startup_failed") from None
        self.reader = self.child.stderr if trace else self.child.stdout
        os.set_blocking(self.reader.fileno(), False)
        os.set_blocking(self.child.stdin.fileno(), False)
        self.buffer = bytearray()
        self.max_line_bytes = 4096
        self.reaped = False

    def exited(self):
        if self.reaped:
            return True
        try:
            return (
                os.waitid(
                    os.P_PID, self.child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                )
                is not None
            )
        except ChildProcessError:
            self.reaped = True
            raise CapabilityError("cleanup_failed") from None

    def line(self, deadline):
        while True:
            wait = deadline.check()
            if self.exited():
                raise CapabilityError("helper_exited")
            if b"\n" in self.buffer:
                line, _, rest = self.buffer.partition(b"\n")
                self.buffer = bytearray(rest)
                if len(line) > self.max_line_bytes:
                    raise CapabilityError("protocol_failed")
                return bytes(line)
            if len(self.buffer) > self.max_line_bytes:
                raise CapabilityError("protocol_failed")
            if not select.select([self.reader], [], [], wait)[0]:
                continue
            chunk = os.read(self.reader.fileno(), 4096)
            if not chunk:
                raise CapabilityError("helper_exited")
            self.buffer.extend(chunk)

    def send(self, word, deadline):
        data = word.encode("ascii") + b"\n"
        while True:
            wait = deadline.check()
            if self.exited():
                raise CapabilityError("helper_exited")
            if select.select([], [self.child.stdin], [], wait)[1]:
                try:
                    if os.write(self.child.stdin.fileno(), data) != len(data):
                        raise CapabilityError("protocol_failed")
                    return
                except OSError:
                    raise CapabilityError("protocol_failed") from None

    def stop(self):
        if self.reaped:
            return
        try:
            self.exited()
            os.killpg(self.child.pid, signal.SIGTERM)
            time.sleep(0.25)
            os.killpg(self.child.pid, signal.SIGKILL)
            self.child.wait(timeout=0.5)
            self.reaped = True
        except (OSError, subprocess.TimeoutExpired):
            raise CapabilityError("cleanup_failed") from None
        finally:
            if self.reaped:
                self.reader.close()
                self.child.stdin.close()


def client_environment(session):
    try:
        if not session.inspect().available:
            raise ValueError
        return {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            **session.client_environment(),
        }
    except (OSError, ValueError):
        raise CapabilityError("session_unavailable") from None
