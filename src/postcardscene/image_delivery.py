"""One spawned, token-scoped frame URL with bounded progress/readiness waiting.

Call capture_image_source in a short transaction immediately before start, then
wait_ready while the renderer navigates. Always close the handle (or use with).
No renderer lifecycle, catalog mutation or runtime retry policy is owned here.
"""

import math
import multiprocessing
import secrets
import sys
import time

from postcardscene.filesystem_source import PathPolicy
from postcardscene.image_frame_context import (
    FrameFailure,
    ImageDeliveryError,
    capture_image_source,
    validate_context,
)
from postcardscene.image_frame_server import frame_worker

__all__ = [
    "capture_image_source",
    "start_image_frame",
    "ImageDeliveryError",
    "FrameFailure",
]


class ImageFrameHandle:
    def __init__(self, process, receiver, cancelled, timeout_seconds):
        self._process = process
        self._receiver = receiver
        self._cancelled = cancelled
        self._timeout = timeout_seconds
        self._closed = False
        self._ready = False
        self.url = None

    def _receive(self, startup):
        deadline = time.monotonic() + self._timeout
        while True:
            if self._cancelled is not None and self._cancelled():
                raise ImageDeliveryError(FrameFailure.CANCELLED)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ImageDeliveryError(FrameFailure.TIMEOUT)
            try:
                if not self._receiver.poll(min(0.02, remaining)):
                    continue
                message = self._receiver.recv()
            except Exception:
                raise ImageDeliveryError(FrameFailure.HELPER) from None
            if self._cancelled is not None and self._cancelled():
                raise ImageDeliveryError(FrameFailure.CANCELLED)
            if type(message) is not tuple or len(message) != 2:
                raise ImageDeliveryError(FrameFailure.HELPER)
            kind, value = message
            if (
                startup
                and kind == "listening"
                and type(value) is int
                and 0 < value < 65536
            ):
                return value
            if value is not None or not isinstance(kind, str):
                raise ImageDeliveryError(FrameFailure.HELPER)
            if kind == "progress":
                deadline = time.monotonic() + self._timeout
            elif kind == "ready" and not startup:
                return
            elif kind in {failure.value for failure in FrameFailure}:
                raise ImageDeliveryError(FrameFailure(kind))
            else:
                raise ImageDeliveryError(FrameFailure.HELPER)

    def wait_ready(self):
        """A browser assertion, not proof of Chromium format/frame retention."""
        if self._ready:
            return
        if self._closed:
            raise ImageDeliveryError(FrameFailure.CANCELLED)
        try:
            self._receive(startup=False)
            self._ready = True
        finally:
            self._close_preserving_error()

    def close(self):
        """Idempotent terminate/join/kill/join; kernel D-state may resist reaping."""
        if self._closed:
            return
        self._receiver.close()
        process = self._process
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(0.1)
            if process.is_alive():
                process.kill()
                process.join(0.1)
            if process.is_alive():
                raise ImageDeliveryError(FrameFailure.HELPER)
        process.close()
        self._closed = True

    def _close_preserving_error(self):
        primary = sys.exception()
        try:
            self.close()
        except ImageDeliveryError as error:
            if primary is None:
                raise
            primary.add_note(str(error))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._close_preserving_error()


def start_image_frame(frame, source, policy, *, cancelled=None, timeout_seconds=10.0):
    """Return only after loopback bind; timeout is trusted lack-of-progress policy."""
    validate_context(frame, source)
    if not isinstance(policy, PathPolicy):
        raise ImageDeliveryError(FrameFailure.INVALID)
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ImageDeliveryError(FrameFailure.INVALID)
    if cancelled is not None and cancelled():
        raise ImageDeliveryError(FrameFailure.CANCELLED)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    token = secrets.token_urlsafe(32)
    process = context.Process(
        target=frame_worker, args=(sender, frame, source, policy, token)
    )
    handle = ImageFrameHandle(process, receiver, cancelled, timeout_seconds)
    try:
        try:
            process.start()
        except Exception:
            raise ImageDeliveryError(FrameFailure.HELPER) from None
        finally:
            sender.close()
        port = handle._receive(startup=True)
        handle.url = f"http://127.0.0.1:{port}/{token}/"
        return handle
    except BaseException:
        handle._close_preserving_error()
        raise
