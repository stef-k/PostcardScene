"""Private one-frame HTTP helper. Source I/O runs only in this spawned process."""

import mimetypes
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from postcardscene.filesystem_source import (
    InvalidSource,
    SourceUnavailable,
    open_image_item,
)
from postcardscene.image_frame_context import FrameFailure
from postcardscene.image_frame_page import frame_page
from postcardscene.mounted_source import _mounted_directory, _network_mount_covers

CHUNK_BYTES = 64 * 1024


def _check_open_mount(stream):
    """Also check the pinned file's mount, closing unmount/open fallthrough races."""
    info = Path(f"/proc/self/fdinfo/{stream.fileno()}").read_text()
    mount_id = next(
        line.split()[1] for line in info.splitlines() if line.startswith("mnt_id:")
    )
    mounts = Path("/proc/self/mountinfo").read_text()
    entry = next(
        (line for line in mounts.splitlines() if line.split()[0] == mount_id), ""
    )
    if not entry or not _network_mount_covers(Path("/"), _root_mount_entry(entry)):
        raise SourceUnavailable("Image mount is unavailable.")


def _root_mount_entry(entry):
    # Reuse the existing mount parser/type contract for this exact pinned mount.
    fields = entry.split()
    fields[4] = "/"
    return " ".join(fields)


class FrameServer(HTTPServer):
    def __init__(self, connection, frame, source, policy, token):
        self.connection = connection
        self.frame = frame
        self.source = source
        self.policy = policy
        self.prefix = f"/{token}/"
        self.page, self.csp = frame_page(frame)
        self.terminal = None
        self.last_progress = 0.0
        super().__init__(("127.0.0.1", 0), FrameRequest)

    def progress(self):
        now = time.monotonic()
        if now - self.last_progress >= 0.05:
            self.connection.send(("progress", None))
            self.last_progress = now

    def finish(self, state):
        if self.terminal is None:
            self.terminal = state
            self.connection.send((state, None))

    def handle_error(self, request, client_address):
        # Base server prints a traceback; never emit request/token/path details.
        self.finish(FrameFailure.HELPER.value)


class FrameRequest(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self._headers(404, 0)

    def _headers(self, status, length, content_type="application/octet-stream"):
        self.send_response_only(status)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Security-Policy", self.server.csp)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _valid_request(self):
        expected_host = f"127.0.0.1:{self.server.server_port}"
        origin = self.headers.get("Origin")
        return (
            self.headers.get_all("Host") == [expected_host]
            and origin in (None, f"http://{expected_host}")
            and self.headers.get("Sec-Fetch-Site") not in ("cross-site", "same-site")
            and self.headers.get_all("Content-Length", []) in ([], ["0"])
            and "Transfer-Encoding" not in self.headers
            and self.path.startswith(self.server.prefix)
        )

    def do_GET(self):
        if not self._valid_request():
            self._headers(404, 0)
            return
        route = self.path[len(self.server.prefix) :]
        if route == "":
            self._headers(200, len(self.server.page), "text/html; charset=utf-8")
            self.wfile.write(self.server.page)
            self.server.progress()
        elif route in [f"asset/{i}" for i in range(len(self.server.frame.images))]:
            self._asset(int(route[-1]))
        else:
            self._headers(404, 0)

    def _asset(self, index):
        source = self.server.source
        image = self.server.frame.images[index]
        try:
            if source.kind == "mounted_directory":
                _mounted_directory(source.configuration, self.server.policy)
                self.server.progress()
            with open_image_item(
                source.kind,
                source.configuration,
                self.server.policy,
                image.relative_path,
                image.size_bytes,
                image.mtime_ns,
            ) as stream:
                if source.kind == "mounted_directory":
                    _check_open_mount(stream)
                self.server.progress()
                mime = mimetypes.types_map.get(Path(image.relative_path).suffix.lower())
                self._headers(200, image.size_bytes, mime or "application/octet-stream")
                remaining = image.size_bytes
                while remaining:
                    chunk = stream.read(min(CHUNK_BYTES, remaining))
                    if not chunk:
                        raise InvalidSource("Image changed during transfer.")
                    self.server.progress()
                    self.wfile.write(chunk)
                    self.server.progress()
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.server.finish(FrameFailure.PRESENTATION.value)
        except InvalidSource:
            self.server.finish(FrameFailure.UNSAFE.value)
        except (SourceUnavailable, OSError, RuntimeError):
            self.server.finish(FrameFailure.UNAVAILABLE.value)

    def do_POST(self):
        route = self.path[len(self.server.prefix) :]
        if not self._valid_request() or route not in ("ready", "failed"):
            self._headers(404, 0)
            return
        self._headers(204, 0)
        self.server.finish(
            "ready" if route == "ready" else FrameFailure.PRESENTATION.value
        )


def frame_worker(connection, frame, source, policy, token):
    try:
        with FrameServer(connection, frame, source, policy, token) as server:
            connection.send(("listening", server.server_port))
            while server.terminal is None:
                server.handle_request()
    except Exception:
        try:
            connection.send((FrameFailure.HELPER.value, None))
        except (OSError, EOFError):
            pass
    finally:
        connection.close()
