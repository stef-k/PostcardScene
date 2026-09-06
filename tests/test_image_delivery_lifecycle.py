"""Fault injection at the disposable process and HTTP operation boundaries."""

import functools
import http.client
import multiprocessing
import socket
import threading
from contextlib import contextmanager
from dataclasses import replace
from urllib.parse import urlsplit

import pytest

from postcardscene import image_delivery as delivery
from postcardscene import image_frame_server as server
from postcardscene.filesystem_source import PathPolicy
from postcardscene.image_frame_context import (
    FrameFailure,
    ImageDeliveryError,
    ImageSource,
)
from postcardscene.image_selection import ImageFrame, SelectedImage


@pytest.fixture
def prepared(tmp_path):
    path = tmp_path / "image.png"
    path.write_bytes(b"original" * 20000)
    info = path.stat()
    image = SelectedImage(
        1, path.name, info.st_size, info.st_mtime_ns, 10, 20, "portrait"
    )
    return (
        ImageFrame((image,), "contain"),
        ImageSource(1, "local_directory", str(tmp_path), True),
        PathPolicy([tmp_path]),
    )


def fault_worker(connection, *args, mode):
    if mode == "eof":
        connection.close()
    elif mode == "crash":
        import os

        os._exit(2)
    elif mode == "malformed":
        connection.send({"unexpected": "message"})
    elif mode == "corrupt":
        connection.send_bytes(b"not a pickled message")
    elif mode == "impossible":
        connection.send(("ready", None))
    elif mode == "bind":

        def fail_bind(self):
            raise OSError("private diagnostic")

        server.FrameServer.server_bind = fail_bind
        server.frame_worker(connection, *args)
    elif mode == "stall":
        import signal

        signal.signal(signal.SIGTERM, signal.SIG_IGN)

        @contextmanager
        def blocked(*a, **kw):
            threading.Event().wait()
            yield

        # Both source kinds block only after the helper has accepted the asset.
        server._mounted_directory = lambda *a: None
        server.open_image_item = blocked
        server.frame_worker(connection, *args)


@pytest.mark.parametrize(
    "mode", ["eof", "crash", "malformed", "corrupt", "impossible", "bind"]
)
def test_startup_failures_are_safe_and_reaped(prepared, monkeypatch, mode):
    monkeypatch.setattr(
        delivery, "frame_worker", functools.partial(fault_worker, mode=mode)
    )
    before = {p.pid for p in multiprocessing.active_children()}
    with pytest.raises(ImageDeliveryError) as error:
        delivery.start_image_frame(*prepared)
    assert error.value.reason == FrameFailure.HELPER
    assert "private" not in str(error.value)
    assert {p.pid for p in multiprocessing.active_children()} == before


@pytest.mark.parametrize("kind", ["local_directory", "mounted_directory"])
def test_stalled_asset_timeout_reaps_helper(prepared, monkeypatch, kind):
    monkeypatch.setattr(
        delivery, "frame_worker", functools.partial(fault_worker, mode="stall")
    )
    frame, source, policy = prepared
    handle = delivery.start_image_frame(frame, replace(source, kind=kind), policy)
    pid = handle._process.pid
    url = urlsplit(handle.url)
    with socket.create_connection((url.hostname, url.port), timeout=2) as client:
        client.sendall(
            f"GET {url.path}asset/0 HTTP/1.1\r\nHost: {url.netloc}\r\n\r\n".encode()
        )
        handle._timeout = 0.15
        with pytest.raises(ImageDeliveryError) as error:
            handle.wait_ready()
        assert error.value.reason == FrameFailure.TIMEOUT
    assert pid not in {p.pid for p in multiprocessing.active_children()}


def test_crash_after_startup(prepared):
    handle = delivery.start_image_frame(*prepared)
    handle._process.kill()
    with pytest.raises(ImageDeliveryError) as error:
        handle.wait_ready()
    assert error.value.reason == FrameFailure.HELPER
    assert handle._closed


@pytest.mark.parametrize(
    "first,second,expected",
    [("ready", "failed", "ready"), ("failed", "ready", "presentation")],
)
def test_first_callback_wins_and_stream_reads_are_bounded(
    prepared, monkeypatch, first, second, expected
):
    frame, source, policy = prepared
    original_open = server.open_image_item
    reads = []

    @contextmanager
    def measured_open(*args, **kwargs):
        with original_open(*args, **kwargs) as stream:

            class Reader:
                def read(self, size):
                    reads.append(size)
                    assert 0 < size <= server.CHUNK_BYTES
                    return stream.read(size)

            yield Reader()

    monkeypatch.setattr(server, "open_image_item", measured_open)
    receiver, sender = multiprocessing.Pipe(duplex=False)
    with server.FrameServer(
        sender, frame, source, policy, "controlled-token"
    ) as helper:

        def serve():
            for _ in range(4):
                helper.handle_request()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        for route, method in [
            ("asset/0", "GET"),
            (first, "POST"),
            (second, "POST"),
            (first, "POST"),
        ]:
            client = http.client.HTTPConnection(
                "127.0.0.1", helper.server_port, timeout=2
            )
            client.request(method, "/controlled-token/" + route)
            response = client.getresponse()
            data = response.read()
            assert response.status == (200 if method == "GET" else 204)
            if method == "GET":
                assert data == b"original" * 20000
            client.close()
        thread.join(2)
        assert not thread.is_alive()
        assert helper.terminal == expected
    sender.close()
    messages = []
    while True:
        try:
            messages.append(receiver.recv())
        except EOFError:
            break
    receiver.close()
    assert [message for message in messages if message[0] != "progress"] == [
        (expected, None)
    ]
    assert len(reads) == 3


def test_progress_extends_timeout_beyond_total_duration(monkeypatch):
    # Deterministic clock/IPC seam: four seconds of useful work with a one-second
    # inactivity limit must succeed. No wall-clock timing or browser assumption.
    clock = [0.0]
    messages = iter([("progress", None)] * 5 + [("ready", None)])

    class Receiver:
        def poll(self, timeout):
            clock[0] += 0.7
            return True

        def recv(self):
            return next(messages)

    monkeypatch.setattr(delivery.time, "monotonic", lambda: clock[0])
    handle = delivery.ImageFrameHandle(None, Receiver(), None, 1)
    handle._receive(startup=False)
    assert clock[0] > 4


def test_pinned_mount_identity_refuses_fallthrough(prepared, monkeypatch):
    _, source, _ = prepared
    path = source.path + "/image.png"
    original_read = server.Path.read_text
    with open(path, "rb") as stream:

        def mountinfo(file, *args, **kwargs):
            if str(file).startswith("/proc/self/fdinfo/"):
                return "mnt_id:\t42\n"
            if str(file) == "/proc/self/mountinfo":
                return "42 1 0:1 / /mounted rw - nfs server:/share rw\n"
            return original_read(file, *args, **kwargs)

        monkeypatch.setattr(server.Path, "read_text", mountinfo)
        server._check_open_mount(stream)
        monkeypatch.setattr(
            server.Path, "read_text", lambda p: mountinfo(p).replace(" nfs ", " ext4 ")
        )
        with pytest.raises(server.SourceUnavailable):
            server._check_open_mount(stream)
