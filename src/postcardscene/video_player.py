"""Single-video native-Wayland owner; serialized calls by the runtime owner."""

import fcntl
import json
import math
import os
import select
import signal
import socket
import stat
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from postcardscene.graphics._capability import (
    CapabilityError,
    Deadline,
    client_environment,
    command_words,
)


class PlaybackError(CapabilityError):
    """Fixed playback reason compatible with ContentSurfaces cleanup."""


@dataclass(frozen=True)
class PlaybackSnapshot:
    state: str
    reason: str
    cleanup_failed: bool
    position_seconds: float | None = None
    duration_seconds: float | None = None
    seekable: bool = False
    absolute_seekable: bool = False


def _seconds(value):
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


@dataclass(frozen=True)
class PlaybackStatus:
    state: str
    reason: str
    cleanup_failed: bool

    def public_diagnostics(self):
        return vars(self).copy()


FLAGS = (
    "--no-config",
    "--gpu-context=wayland",
    "--vo=gpu",
    "--fullscreen",
    "--border=no",
    "--input-default-bindings=no",
    "--input-vo-keyboard=no",
    "--input-cursor=no",
    "--input-terminal=no",
    "--osc=no",
    "--load-scripts=no",
    "--terminal=no",
    "--audio=no",
    "--audio-file-auto=no",
    "--sub-auto=no",
    "--autoload-files=no",
    "--access-references=no",
    "--idle=yes",
    "--keep-open=no",
)


class MpvController:
    """Prepare duplicates a borrowed #72 FD; stop retires all owned authority.

    Construct ContentSurfaces before prepare. Calls are serialized, and the owner
    polls status/reconcile regularly; no background worker or retry is created.
    Launchers must exec/wait and retain descendants in the child process group.
    Systemd is the outer runtime-crash cleanup boundary.
    """

    def __init__(self, session, command):
        self._lock = threading.RLock()
        self._retiring = threading.Event()
        self._generation = 0
        self._request_id = 1
        self._pending = None
        self._response = None
        self.session = session
        self.command = command_words(command)
        self._media = None
        self._child = None
        self._ipc = None
        self._buffer = bytearray()
        self._state = "stopped"
        self._reason = "stopped"
        self._cleanup_failed = False
        self._acknowledged = False

    def prepare(self, descriptor):
        with self._lock:
            self._prepare(descriptor)
            self._retiring.clear()

    def _prepare(self, descriptor):
        if self._state != "stopped" or self._child is not None:
            raise PlaybackError("already_owned")
        try:
            if type(descriptor) is not int:
                raise ValueError
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError
            if fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
                raise ValueError
            self._media = os.dup(descriptor)
        except (OSError, ValueError):
            raise PlaybackError("invalid_media") from None
        self._state = self._reason = "prepared"

    @property
    def status(self):
        with self._lock:
            return self._status()

    def _status(self):
        if self._state in ("playing", "paused"):
            try:
                self._poll(0)
            except PlaybackError as error:
                self._fail(error)
            except OSError:
                self._fail(PlaybackError("process_failed"))
        return PlaybackStatus(self._state, self._reason, self._cleanup_failed)

    def ensure_started(self, *, timeout_seconds=10, cancelled=None):
        with self._lock:
            self._ensure_started(
                timeout_seconds=timeout_seconds,
                cancelled=lambda: (
                    self._retiring.is_set() or bool(cancelled and cancelled())
                ),
            )

    def _ensure_started(self, *, timeout_seconds, cancelled):
        try:
            deadline = Deadline(timeout_seconds, cancelled)
            deadline.check()
            if self._state in ("playing", "paused", "ended"):
                if self.status.state == "failed":
                    raise PlaybackError(self._reason)
                return
            if self._state != "prepared":
                raise PlaybackError("not_prepared")
            self._launch(client_environment(self.session))
            while not (self._state in ("playing", "ended") and self._acknowledged):
                self._poll(deadline.check())
        except CapabilityError as error:
            failure = PlaybackError(error.reason)
            self._fail(failure)
            raise failure from None
        except OSError:
            failure = PlaybackError("startup_failed")
            self._fail(failure)
            raise failure from None

    @contextmanager
    def _control(self, timeout_seconds, cancelled, *, allow_retired=False):
        generation = self._generation
        acquired = False
        try:
            deadline = Deadline(
                timeout_seconds,
                lambda: (
                    (self._retiring.is_set() and not allow_retired)
                    or generation != self._generation
                    or bool(cancelled and cancelled())
                ),
            )
            while not acquired:
                acquired = self._lock.acquire(timeout=deadline.check())
            deadline.check()
            yield deadline
        except CapabilityError as error:
            failure = PlaybackError(error.reason)
            if (
                acquired
                and generation == self._generation
                and error.reason
                in (
                    "timeout",
                    "cancelled",
                    "protocol_failed",
                    "process_exited",
                    "load_failed",
                )
            ):
                self._fail(failure)
            raise failure from None
        except OSError:
            failure = PlaybackError("protocol_failed")
            if acquired:
                self._fail(failure)
            raise failure from None
        finally:
            if acquired:
                self._lock.release()

    def _active(self):
        if self._state not in ("playing", "paused"):
            raise PlaybackError("not_active")

    def _request(self, command, deadline, *, property_read=False):
        deadline.check()
        self._active()
        self._request_id += 1
        self._pending = self._request_id
        self._response = None
        data = (
            json.dumps({"command": command, "request_id": self._pending}).encode()
            + b"\n"
        )
        while data:
            wait = deadline.check()
            if select.select([], [self._ipc], [], wait)[1]:
                sent = self._ipc.send(data)
                if not sent:
                    raise PlaybackError("protocol_failed")
                data = data[sent:]
        while self._response is None:
            self._poll(deadline.check())
            if self._state == "ended":
                raise PlaybackError("not_active")
        deadline.check()
        response = self._response
        self._pending = self._response = None
        if response["error"] == "property unavailable" and property_read:
            return None
        if response["error"] != "success":
            raise PlaybackError("command_failed")
        return response.get("data")

    def _snapshot(self, deadline):
        if self._state not in ("playing", "paused"):
            return PlaybackSnapshot(self._state, self._reason, self._cleanup_failed)
        values = {}
        try:
            for name in ("pause", "time-pos", "duration", "seekable"):
                values[name] = self._request(
                    ["get_property", name], deadline, property_read=True
                )
        except PlaybackError as error:
            if error.reason != "not_active" or self._state != "ended":
                raise
            return PlaybackSnapshot(self._state, self._reason, self._cleanup_failed)
        if type(values["pause"]) is not bool:
            raise PlaybackError("protocol_failed")
        self._state = self._reason = "paused" if values["pause"] else "playing"
        duration = _seconds(values["duration"])
        seekable = values["seekable"] is True
        return PlaybackSnapshot(
            self._state,
            self._reason,
            self._cleanup_failed,
            _seconds(values["time-pos"]),
            duration,
            seekable,
            seekable and duration is not None and duration > 0,
        )

    def snapshot(self, *, timeout_seconds=1, cancelled=None):
        with self._control(timeout_seconds, cancelled, allow_retired=True) as deadline:
            return self._snapshot(deadline)

    def _pause(self, paused, timeout_seconds, cancelled):
        with self._control(timeout_seconds, cancelled) as deadline:
            self._request(["set_property", "pause", paused], deadline)
            return self._snapshot(deadline)

    def pause(self, *, timeout_seconds=1, cancelled=None):
        return self._pause(True, timeout_seconds, cancelled)

    def resume(self, *, timeout_seconds=1, cancelled=None):
        return self._pause(False, timeout_seconds, cancelled)

    def _seek(self, seconds, absolute, timeout_seconds, cancelled):
        if type(seconds) not in (int, float):
            raise PlaybackError("invalid_seek")
        try:
            valid = math.isfinite(seconds) and (
                seconds >= 0 if absolute else abs(seconds) <= 3600
            )
        except OverflowError:
            valid = False
        if not valid:
            raise PlaybackError("invalid_seek")
        with self._control(timeout_seconds, cancelled) as deadline:
            state = self._snapshot(deadline)
            self._active()
            if not state.seekable or (absolute and not state.absolute_seekable):
                raise PlaybackError("seek_unavailable")
            if absolute and seconds > state.duration_seconds:
                raise PlaybackError("invalid_seek")
            mode = "absolute+keyframes" if absolute else "relative+keyframes"
            self._request(["seek", seconds, mode], deadline)
            return self._snapshot(deadline)

    def seek_relative(self, seconds, *, timeout_seconds=1, cancelled=None):
        return self._seek(seconds, False, timeout_seconds, cancelled)

    def seek_absolute(self, seconds, *, timeout_seconds=1, cancelled=None):
        return self._seek(seconds, True, timeout_seconds, cancelled)

    def _launch(self, environment):
        parent, child = socket.socketpair()
        self._ipc = parent
        self._state = self._reason = "starting"
        try:
            self._child = subprocess.Popen(
                [
                    *self.command,
                    *FLAGS,
                    f"--input-ipc-client=fd://{child.fileno()}",
                    "--",
                    f"fd://{self._media}",
                ],
                env=environment,
                shell=False,
                start_new_session=True,
                close_fds=True,
                pass_fds=(self._media, child.fileno()),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        finally:
            child.close()
            os.close(self._media)
            self._media = None
        parent.setblocking(False)
        # One fixed handshake, correlated independently of asynchronous events.
        data = b'{"command":["get_property","idle-active"],"request_id":1}\n'
        if parent.send(data) != len(data):
            raise PlaybackError("protocol_failed")

    def _poll(self, wait):
        # A bounded batch prevents event floods from monopolizing the runtime.
        for _ in range(32):
            if b"\n" in self._buffer:
                line, _, self._buffer = self._buffer.partition(b"\n")
                self._message(line)
                continue
            if len(self._buffer) > 8192:
                raise PlaybackError("protocol_failed")
            if not select.select([self._ipc], [], [], wait)[0]:
                if self._exited():
                    raise PlaybackError("process_exited")
                return
            wait = 0
            try:
                chunk = self._ipc.recv(8193 - len(self._buffer))
            except OSError:
                raise PlaybackError("protocol_failed") from None
            if not chunk:
                if self._state == "ended":
                    return
                raise PlaybackError("process_exited")
            self._buffer.extend(chunk)

    def _message(self, line):
        try:
            if len(line) > 8192:
                raise ValueError
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError):
            raise PlaybackError("protocol_failed") from None
        if "request_id" in message and self._acknowledged:
            if (
                type(message["request_id"]) is not int
                or message["request_id"] != self._pending
                or not isinstance(message.get("error"), str)
                or self._response is not None
            ):
                raise PlaybackError("protocol_failed")
            self._response = message
            return
        if "request_id" in message:
            if (
                type(message["request_id"]) is not int
                or message["request_id"] != 1
                or self._acknowledged
                or message.get("error") != "success"
                or type(message.get("data")) is not bool
            ):
                raise PlaybackError("protocol_failed")
            self._acknowledged = True
            return
        event = message.get("event")
        if event == "file-loaded" and self._state == "starting":
            self._state = self._reason = "playing"
        elif event == "end-file":
            if message.get("reason") != "eof" or self._state not in (
                "playing",
                "paused",
            ):
                raise PlaybackError("load_failed")
            self._state = self._reason = "ended"
        elif event not in (
            "start-file",
            "audio-reconfig",
            "video-reconfig",
            "playback-restart",
            "idle",
            "seek",
            "shutdown",
        ):
            raise PlaybackError("protocol_failed")

    def _exited(self):
        # Preserve the leader PID until group retirement, even after a crash.
        return (
            os.waitid(os.P_PID, self._child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            is not None
        )

    def _fail(self, error):
        try:
            self.stop()
        except PlaybackError:
            error.cleanup_failed = True
        self._state = "failed"
        self._reason = error.reason

    def stop(self):
        self._generation += 1
        self._retiring.set()
        with self._lock:
            self._stop()

    def _stop(self):
        if self._media is not None:
            os.close(self._media)
            self._media = None
        if self._ipc is not None:
            self._ipc.close()
            self._ipc = None
        self._pending = self._response = None
        self._buffer.clear()
        self._acknowledged = False
        if self._child is not None:
            try:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(self._child.pid, sig)
                    except ProcessLookupError:
                        pass
                    if sig == signal.SIGTERM:
                        time.sleep(0.1)
                self._child.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                self._cleanup_failed = True
                self._state = "failed"
                self._reason = "cleanup_failed"
                error = PlaybackError("cleanup_failed")
                error.cleanup_failed = True
                raise error from None
            self._child = None
        self._cleanup_failed = False
        self._state = self._reason = "stopped"
