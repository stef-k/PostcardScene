"""Controlled HTTP assertions only; these tests do not run Chromium."""

import http.client
import io
import multiprocessing
import os
import pickle
from dataclasses import replace
from urllib.parse import urlsplit

import pytest
from PIL import Image

from postcardscene.domain import Source
from postcardscene.image_delivery import (
    FrameFailure,
    ImageDeliveryError,
    capture_image_source,
    start_image_frame,
)
from postcardscene.image_selection import ImageFrame, SelectedImage


def selected(source_id, path):
    info = path.stat()
    return SelectedImage(
        source_id, path.name, info.st_size, info.st_mtime_ns, 10, 20, "portrait"
    )


@pytest.fixture
def prepared(catalog):
    db, source_id, root, policy = catalog
    path = root / "private-photo.jpg"
    path.write_bytes(b"original image bytes")
    frame = ImageFrame((selected(source_id, path),), "contain")
    with db.transaction() as session:
        source = capture_image_source(session, frame)
    return frame, source, policy


def request(handle, route="", method="GET", body=None, headers=None):
    url = urlsplit(handle.url)
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=3)
    connection.request(method, url.path + route, body=body, headers=headers or {})
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


@pytest.mark.parametrize("count,fit", [(1, "contain"), (1, "cover"), (2, "contain")])
def test_original_bytes_page_and_browser_assertion(catalog, count, fit):
    db, source_id, root, policy = catalog
    originals = []
    images = []
    for index, fmt in enumerate(("JPEG", "PNG")[:count]):
        buffer = io.BytesIO()
        Image.new("RGB", (10, 20), "red").save(
            buffer, format=fmt, icc_profile=b"profile-preserved"
        )
        path = root / f"private-photo-{index}.{fmt.lower()}"
        path.write_bytes(buffer.getvalue())
        originals.append(buffer.getvalue())
        images.append(selected(source_id, path))
    frame = ImageFrame(tuple(images), fit)
    with db.transaction() as session:
        source = capture_image_source(session, frame)
    assert pickle.loads(pickle.dumps(source)) == source
    with start_image_frame(frame, source, policy) as handle:
        assert handle._process._start_method == "spawn"
        assert urlsplit(handle.url).hostname == "127.0.0.1"
        status, headers, page = request(handle)
        assert status == 200
        html = page.decode()
        assert f"object-fit: {fit}" in html
        assert f"repeat({count}, minmax(0, 1fr))" in html
        assert "image-orientation: from-image" in html
        assert html.count("<img ") == count
        assert "Promise.all(images.map(present))" in html
        assert "await image.decode()" in html
        assert "private-photo" not in html and str(root) not in html
        assert "<button" not in html
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        assert "'unsafe-inline'" not in headers["Content-Security-Policy"]
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "Access-Control-Allow-Origin" not in headers
        for index, expected in enumerate(originals):
            assert request(handle, f"asset/{index}")[2] == expected
        assert handle._process.is_alive()  # Byte delivery alone is not readiness.
        assert request(handle, "ready", "POST")[0] == 204
        handle.wait_ready()
        handle.wait_ready()
        assert handle._closed


@pytest.mark.parametrize(
    "route,method,body,headers",
    [
        ("asset/2", "GET", None, {}),
        ("asset/0?path=secret", "GET", None, {}),
        ("../private-photo.jpg", "GET", None, {}),
        ("ready", "GET", None, {}),
        ("", "DELETE", None, {}),
        ("ready", "POST", b"payload", {}),
        ("failed?x=1", "POST", None, {}),
        ("", "GET", None, {"Host": "evil.invalid"}),
        ("ready", "POST", None, {"Origin": "http://evil.invalid"}),
        ("ready", "POST", None, {"Transfer-Encoding": "chunked"}),
    ],
)
def test_rejected_requests_do_not_grant_authority(
    prepared, route, method, body, headers
):
    with start_image_frame(*prepared) as handle:
        status, _, data = request(handle, route, method, body, headers)
        assert status == 404 and data == b""
        assert request(handle, "ready", "POST")[0] == 204
        handle.wait_ready()


def test_unknown_token_on_all_routes(prepared):
    with start_image_frame(*prepared) as handle:
        original = handle.url
        handle.url = original.rsplit("/", 2)[0] + "/wrong-token/"
        for route, method in [
            ("", "GET"),
            ("asset/0", "GET"),
            ("ready", "POST"),
            ("failed", "POST"),
        ]:
            assert request(handle, route, method)[0] == 404
        handle.url = original
        request(handle, "failed", "POST")
        with pytest.raises(ImageDeliveryError) as error:
            handle.wait_ready()
        assert error.value.reason == FrameFailure.PRESENTATION
        assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "change", ["empty", "oversized", "duplicate", "mixed", "fit", "entry"]
)
def test_invalid_frames_never_spawn(prepared, monkeypatch, change):
    frame, source, policy = prepared
    image = frame.images[0]
    frames = {
        "empty": ImageFrame((), "contain"),
        "oversized": ImageFrame((image,) * 3, "contain"),
        "duplicate": ImageFrame((image, image), "contain"),
        "mixed": ImageFrame(
            (image, replace(image, source_id=image.source_id + 1)), "contain"
        ),
        "fit": ImageFrame((image,), "invalid"),
        "entry": ImageFrame((object(),), "contain"),
    }
    monkeypatch.setattr(
        multiprocessing, "get_context", lambda *a: pytest.fail("spawned")
    )
    with pytest.raises(ImageDeliveryError) as error:
        start_image_frame(frames[change], source, policy)
    assert error.value.reason == FrameFailure.INVALID


@pytest.mark.parametrize("change", ["missing", "disabled", "web", "config"])
def test_invalid_db_source(catalog, prepared, change):
    db, source_id, _, _ = catalog
    frame = prepared[0]
    with db.transaction() as session:
        source = session.get(Source, source_id)
        if change == "missing":
            session.delete(source)
            session.flush()
        elif change == "disabled":
            source.enabled = False
        elif change == "web":
            source.kind = "web_url"
        else:
            source.configuration = {}
        with pytest.raises(ImageDeliveryError) as error:
            capture_image_source(session, frame)
        assert error.value.reason == FrameFailure.INVALID


def test_mounted_capture_is_db_only_and_unmounted_local_is_not_served(
    catalog, prepared, monkeypatch
):
    db, source_id, _, _ = catalog
    frame, _, policy = prepared
    with db.transaction() as session:
        source = session.get(Source, source_id)
        source.kind = "mounted_directory"
        with monkeypatch.context() as patch:
            patch.setattr(
                "pathlib.Path.resolve", lambda *a, **kw: pytest.fail("parent path I/O")
            )
            patch.setattr(os, "open", lambda *a, **kw: pytest.fail("parent open"))
            snapshot = capture_image_source(session, frame)
    with start_image_frame(frame, snapshot, policy) as handle:
        # The file exists locally, but is not on NFS/CIFS.
        with pytest.raises(http.client.RemoteDisconnected):
            request(handle, "asset/0")
        with pytest.raises(ImageDeliveryError) as error:
            handle.wait_ready()
        assert error.value.reason == FrameFailure.UNAVAILABLE


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan"), "1"])
def test_invalid_timeout(prepared, timeout):
    with pytest.raises(ImageDeliveryError) as error:
        start_image_frame(*prepared, timeout_seconds=timeout)
    assert error.value.reason == FrameFailure.INVALID


def test_cancel_and_early_close(prepared):
    from threading import Event

    cancelled = Event()
    handle = start_image_frame(*prepared, cancelled=cancelled.is_set)
    pid = handle._process.pid
    cancelled.set()
    with pytest.raises(ImageDeliveryError) as error:
        handle.wait_ready()
    assert error.value.reason == FrameFailure.CANCELLED
    handle.close()
    assert pid not in [child.pid for child in multiprocessing.active_children()]
    handle = start_image_frame(*prepared)
    handle.close()
    handle.close()
