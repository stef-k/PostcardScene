"""Real child/socket/FD evidence without HDMI or codec support claims."""

import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from postcardscene.video_player import MpvController, PlaybackError


@pytest.fixture
def player(tmp_path):
    script = tmp_path / "player.py"
    script.write_text("""
import json, os, socket, sys, time
args = sys.argv[1:]
assert "--audio=no" in args and "--no-config" in args
assert "--gpu-context=wayland" in args and "--load-scripts=no" in args
assert "--osc=no" in args and "--input-terminal=no" in args
assert "--access-references=no" in args
assert os.environ["WAYLAND_DISPLAY"] == "owned-wayland"
fd = int(args[-1].removeprefix("fd://"))
mode = os.read(fd, 100).decode()
ipc = int(next(a for a in args if a.startswith("--input-ipc-client=")).split("fd://")[1])
for number in range(3, 256):
    if number not in (fd, ipc):
        try:
            os.fstat(number)
        except OSError:
            continue
        raise AssertionError("unexpected inherited fd")
s = socket.socket(fileno=ipc)
f = s.makefile("rb")
request = json.loads(f.readline())
assert request == {"command": ["get_property", "idle-active"], "request_id": 1}
def send(value):
    s.sendall(json.dumps(value).encode() + b"\\n")
if mode == "exit":
    sys.exit(1)
if mode == "timeout":
    time.sleep(10)
if mode == "malformed":
    s.sendall(b"{no\\n")
elif mode == "oversized":
    s.sendall(b"x" * 9000)
elif mode == "wrong-id":
    send({"request_id": 2, "error": "success", "data": False})
elif mode == "unknown":
    send({"event": "unknown"})
elif mode == "corrupt":
    send({"event": "end-file", "reason": "error"})
else:
    send({"request_id": 1, "error": "success", "data": False})
    send({"event": "file-loaded"})
    if mode == "eof":
        send({"event": "end-file", "reason": "eof"})
    if mode == "crash":
        time.sleep(0.1)
        sys.exit(1)
time.sleep(10)
""")
    session = SimpleNamespace(
        inspect=lambda: SimpleNamespace(available=True),
        client_environment=lambda: {"WAYLAND_DISPLAY": "owned-wayland"},
    )
    controller = MpvController(session, (sys.executable, str(script)))

    def prepare(mode):
        path = tmp_path / "video"
        path.write_text(mode)
        with path.open("rb") as media:
            controller.prepare(media.fileno())
        # Caller lifetime and pathname no longer supply player authority.
        path.unlink()
        return controller

    yield prepare
    controller.stop()


def test_load_pinned_fd_and_fresh_process(player):
    controller = player("playing")
    with pytest.raises(PlaybackError, match="already_owned"):
        controller.prepare(0)
    controller.ensure_started(timeout_seconds=1)
    assert controller.status.state == "playing"
    child = controller._child
    controller.ensure_started()
    assert controller._child is child
    controller.stop()
    assert child.returncode is not None
    assert controller._ipc is None and controller._media is None
    controller.stop()
    player("playing").ensure_started(timeout_seconds=1)
    assert controller._child.pid != child.pid


def test_eof_is_completion_without_reloading(player):
    controller = player("eof")
    controller.ensure_started(timeout_seconds=1)
    for _ in range(20):
        if controller.status.state == "ended":
            break
        time.sleep(0.01)
    assert controller.status.public_diagnostics() == {
        "state": "ended",
        "reason": "ended",
        "cleanup_failed": False,
    }
    child = controller._child
    controller.ensure_started()
    assert controller._child is child


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("exit", "process_exited"),
        ("timeout", "timeout"),
        ("malformed", "protocol_failed"),
        ("oversized", "protocol_failed"),
        ("wrong-id", "protocol_failed"),
        ("unknown", "protocol_failed"),
        ("corrupt", "load_failed"),
    ],
)
def test_load_failures_retire_authority(player, mode, reason):
    controller = player(mode)
    with pytest.raises(PlaybackError, match=reason):
        controller.ensure_started(timeout_seconds=0.3)
    assert controller.status.reason == reason
    assert controller._child is None and controller._ipc is None
    assert controller._media is None


def test_cancel_and_crash(player):
    controller = player("timeout")
    start = time.monotonic()
    with pytest.raises(PlaybackError, match="cancelled"):
        controller.ensure_started(cancelled=lambda: time.monotonic() - start > 0.1)
    assert controller._child is None
    controller.stop()
    player("crash").ensure_started(timeout_seconds=1)
    time.sleep(0.2)
    assert controller.status.state == "failed"
    assert controller._child is None


def test_cleanup_failure_retains_child_for_retry(player, monkeypatch):
    controller = player("playing")
    controller.ensure_started(timeout_seconds=1)
    child = controller._child
    wait = child.wait

    def stuck(*args, **kwargs):
        raise subprocess.TimeoutExpired("private", 0.5)

    monkeypatch.setattr(child, "wait", stuck)
    with pytest.raises(PlaybackError, match="cleanup_failed") as error:
        controller.stop()
    assert error.value.cleanup_failed
    assert controller.status.cleanup_failed
    assert controller._child is child
    with pytest.raises(PlaybackError):
        controller.prepare(0)
    monkeypatch.setattr(child, "wait", wait)
    controller.stop()
    assert controller.status.state == "stopped"


def test_prepared_stop_closes_duplicate(player):
    controller = player("playing")
    descriptor = controller._media
    controller.stop()
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_content_surfaces_retire_video_before_replacement(player):
    from postcardscene.graphics.chromium import BrowserContext
    from postcardscene.graphics.surfaces import ContentClass, ContentSurfaces

    controller = player("playing")
    controller.stop()
    calls = []

    def browser(context):
        def start(**kwargs):
            assert controller._child is None
            calls.append(context)

        return SimpleNamespace(
            context=context,
            session=controller.session,
            stop=lambda: None,
            ensure_started=start,
        )

    surfaces = ContentSurfaces(
        browser(BrowserContext.TRUSTED_IMAGE),
        browser(BrowserContext.UNTRUSTED_WEB),
        controller,
    )
    player("playing")
    surfaces.select(ContentClass.VIDEO, timeout_seconds=1)
    child = controller._child
    surfaces.select(ContentClass.TRUSTED_IMAGE)
    assert child.returncode is not None
    assert calls == [BrowserContext.TRUSTED_IMAGE]
