"""Bounded execution of the three fixed, direct host panel tools."""

import os
import selectors
import subprocess
import time
from collections.abc import Callable

from ._types import Kind, PowerError, Reason

Cancelled = Callable[[], bool]
TOOLS = {
    Kind.CEC: "/usr/bin/cec-ctl",
    Kind.DDC: "/usr/bin/ddcutil",
    Kind.SIGNAL: "/usr/bin/wlopm",
}
OUTPUT_LIMIT = 16384
COMMAND_TIMEOUT = 3.0
REAP_TIMEOUT = 0.25


class Command:
    """Serialized owner. Failed reaping latches authority until process restart."""

    def __init__(self, kind: Kind):
        self.kind = kind
        self.process = None
        self.cleanup_failed = False

    def run(
        self, arguments: list[str], environment: dict[str, str], cancelled: Cancelled
    ) -> bytes:
        if self.cleanup_failed:
            raise PowerError(Reason.CLEANUP_FAILED, cleanup_failed=True)
        if cancelled():
            raise PowerError(Reason.CANCELLED)
        try:
            self.process = subprocess.Popen(
                [TOOLS[self.kind], *arguments],
                env={**environment, "LC_ALL": "C"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
            )
        except FileNotFoundError:
            raise PowerError(Reason.TOOL_MISSING) from None
        except OSError:
            raise PowerError(Reason.TOOL_FAILED) from None
        failure = None
        captured = b""
        try:
            captured = _capture(self.process, cancelled)
        except PowerError as error:
            failure = error
        except OSError:
            failure = PowerError(Reason.TOOL_FAILED)
        finally:
            self._cleanup()
        if self.cleanup_failed:
            raise PowerError(
                failure.reason if failure else Reason.CLEANUP_FAILED,
                cleanup_failed=True,
            )
        if failure:
            raise failure
        return captured

    def _cleanup(self):
        process = self.process
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=REAP_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            self.cleanup_failed = True
        finally:
            try:
                process.stdout.close()
            except OSError:
                self.cleanup_failed = True
        if not self.cleanup_failed:
            self.process = None


def _capture(process: subprocess.Popen, cancelled: Cancelled) -> bytes:
    deadline = time.monotonic() + COMMAND_TIMEOUT
    captured = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map() or process.poll() is None:
            if cancelled():
                raise PowerError(Reason.CANCELLED)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PowerError(Reason.TIMEOUT)
            if not selector.get_map():
                try:
                    process.wait(timeout=min(0.05, remaining))
                except subprocess.TimeoutExpired:
                    pass
                continue
            if not selector.select(min(0.05, remaining)):
                continue
            chunk = os.read(
                process.stdout.fileno(), min(4096, OUTPUT_LIMIT + 1 - len(captured))
            )
            if not chunk:
                selector.unregister(process.stdout)
            captured.extend(chunk)
            if len(captured) > OUTPUT_LIMIT:
                raise PowerError(Reason.OVERSIZED)
    if process.returncode:
        raise PowerError(Reason.TOOL_FAILED)
    return bytes(captured)
