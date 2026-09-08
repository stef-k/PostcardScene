"""Private panel-only AF_UNIX control; one packet and one response per connection."""

import json
import os
import socket
import stat
import struct
from dataclasses import asdict
from pathlib import Path
from threading import Event, Thread

from postcardscene.runtime.panel import Outcome

SOCKET_PATH = Path("/run/postcardscene/panel-control.sock")
LIMIT = 1024


def panel_status(coordinator):
    if coordinator is None:
        return {"configured": False, "state": "unavailable", "reason": "not_configured"}
    return {
        "configured": True,
        **asdict(coordinator.status),
        "intentional_signal_sleep": coordinator.intentional_signal_sleep,
    }


class PanelControl:
    """Linux seqpacket framing, owner UID or directory-group primary GID.

    Parent ownership is runtime-user authority, mode 0700 or 0750. A 0750
    directory grants its group socket access (0660); only the owner can replace
    the endpoint. Provisioning chooses group membership, never a request field.
    Existing endpoints are rejected, including stale sockets; only our inode is
    removed. Ancestors must be trusted and cannot be writable by other users.
    """

    def __init__(self, coordinator, stop_event, path=SOCKET_PATH):
        self.coordinator = coordinator
        self.stop_event = stop_event
        self.path = Path(path)
        self._closed = Event()
        self._listener = None
        self._identity = None
        self._group = None
        self.failure = None
        self.thread = Thread(target=self._run, name="panel-control", daemon=True)

    def _validate_directory(self):
        if not self.path.is_absolute() or self.path.resolve() != self.path:
            raise ValueError("Invalid panel socket path.")
        info = self.path.parent.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if (
            os.geteuid() == 0
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or mode not in (0o700, 0o750)
        ):
            raise ValueError("Invalid panel runtime directory authority.")
        for parent in self.path.parent.parents:
            ancestor = parent.stat()
            # A sticky root-owned /tmp is safe for private test directories.
            sticky_root = ancestor.st_uid == 0 and ancestor.st_mode & stat.S_ISVTX
            if ancestor.st_uid not in (0, os.geteuid()) or (
                ancestor.st_mode & 0o022 and not sticky_root
            ):
                raise ValueError("Invalid panel runtime ancestor authority.")
        self._group = info.st_gid if mode == 0o750 else None

    def start(self):
        self._validate_directory()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self._listener = listener
        try:
            listener.bind(str(self.path))
            info = self.path.lstat()
            self._identity = (info.st_dev, info.st_ino)
            if self._group is not None:
                os.chown(self.path, -1, self._group)
            self.path.chmod(0o660 if self._group is not None else 0o600)
            listener.listen(1)
            listener.settimeout(0.1)
            self.thread.start()
        except BaseException:
            listener.close()
            self._remove_owned_path()
            raise

    def _authorized(self, client):
        _, uid, gid = struct.unpack(
            "3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        return uid == os.geteuid() or (self._group is not None and gid == self._group)

    def _response(self, outcome):
        return {
            "version": 1,
            "outcome": outcome,
            "status": panel_status(self.coordinator),
        }

    def _dispatch(self, raw):
        try:
            request = json.loads(raw)
            if (
                type(request) is not dict
                or set(request) != {"version", "action"}
                or type(request["version"]) is not int
                or request["version"] != 1
                or request["action"] not in ("status", "test_on", "test_off")
            ):
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            return self._response("rejected")
        owner = self.coordinator
        if owner is not None and owner.failure is not None:
            return self._response("cleanup_failed")
        if owner is None or self.stop_event.is_set() or self._closed.is_set():
            return self._response("unavailable")
        if request["action"] != "status":
            result = owner.request_test(request["action"] == "test_on")
            if result.done():
                outcome = result.result()
                if outcome == Outcome.REJECTED:
                    return self._response("rejected")
                if outcome == Outcome.CLEANUP_FAILED:
                    return self._response("cleanup_failed")
                if outcome == Outcome.STOPPED:
                    return self._response("unavailable")
        return self._response("accepted")

    def _serve(self, client):
        client.settimeout(0.1)
        try:
            authorized = self._authorized(client)
            raw, _, flags, _ = client.recvmsg(LIMIT)
            if not authorized:
                response = self._response("rejected")
            else:
                response = (
                    self._response("rejected")
                    if flags & socket.MSG_TRUNC or not raw
                    else self._dispatch(raw)
                )
            encoded = json.dumps(response, separators=(",", ":")).encode("ascii")
            if len(encoded) > LIMIT:
                raise RuntimeError("Panel response exceeded protocol bound.")
            client.sendall(encoded)
        except OSError:
            # Disconnected, silent or slow clients cannot hold the worker.
            pass

    def _run(self):
        try:
            while not self._closed.is_set() and not self.stop_event.is_set():
                try:
                    client, _ = self._listener.accept()
                except TimeoutError:
                    continue
                with client:
                    self._serve(client)
        except BaseException:
            if not self._closed.is_set():
                self.failure = "control_failed"
                self.stop_event.set()

    def _remove_owned_path(self):
        if self._identity is None:
            return
        try:
            info = self.path.lstat()
            if (info.st_dev, info.st_ino) == self._identity:
                self.path.unlink()
        except FileNotFoundError:
            pass
        self._identity = None

    def stop(self):
        self._closed.set()
        if self._listener is not None:
            self._listener.close()
        if self.thread.ident is not None:
            self.thread.join(5)
            if self.thread.is_alive():
                raise RuntimeError("Panel control shutdown failed.")
        self._remove_owned_path()
        if self.failure is not None:
            raise RuntimeError("Panel control failed.")
