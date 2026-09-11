"""Controller contracts with an owned fake browser and private CDP peer."""

import json
import os
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest

from postcardscene.graphics.chromium import (
    BrowserContext,
    ChromiumController,
    ChromiumError,
    ChromiumLaunchSpec,
)


@pytest.fixture
def session():
    return SimpleNamespace(
        inspect=lambda: SimpleNamespace(available=True),
        client_environment=lambda: {"WAYLAND_DISPLAY": "wayland-0"},
    )


@pytest.fixture
def browser(tmp_path, session):
    root = tmp_path / "image"
    root.mkdir(mode=0o700)
    # This script models a package launcher. It keeps all children in its group.
    script = tmp_path / "browser.py"
    script.write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "while True: time.sleep(1)\n"
    )
    controller = ChromiumController(
        session,
        BrowserContext.TRUSTED_IMAGE,
        ChromiumLaunchSpec((sys.executable, str(script)), root),
    )
    yield controller
    controller.stop()


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:123/frame/token",
        "http://localhost:123/frame/token",
        "http://127.0.0.1/frame/token",
        "http://user@127.0.0.1:123/frame/token",
        "http://127.0.0.1:123/frame/token#fragment",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,token",
        "http://127.0.0.1:123/\\other",
    ],
)
def test_invalid_trusted_url_never_starts_browser(browser, url):
    with pytest.raises(ChromiumError, match="invalid_url") as caught:
        browser.navigate(url)
    assert url not in str(caught.value)
    assert browser._process is None


def test_session_unavailable_prevents_launch(browser, session):
    session.inspect = lambda: SimpleNamespace(available=False)
    with pytest.raises(ChromiumError, match="session_unavailable"):
        browser.ensure_started()
    assert browser._process is None


def test_missing_executable_is_typed_and_releases_profile(browser, session):
    controller = ChromiumController(
        session,
        browser.context,
        replace(browser.spec, command=("/missing-postcardscene-launcher",)),
    )
    with pytest.raises(ChromiumError, match="startup_failed"):
        controller.ensure_started()
    assert controller._profile._lock is None and controller._process is None


def test_startup_timeout_reaps_browser_and_allows_retry(browser):
    before = time.monotonic()
    with pytest.raises(ChromiumError, match="cdp_startup"):
        browser.ensure_started(timeout_seconds=0.1)
    assert time.monotonic() - before < 3
    assert browser._process is None
    browser.stop()


def test_context_roots_cannot_be_reused(browser, session):
    with pytest.raises(ChromiumError, match="invalid_spec"):
        ChromiumController(session, BrowserContext.UNTRUSTED_WEB, browser.spec)
    with pytest.raises(ChromiumError, match="invalid_spec"):
        ChromiumController(
            session,
            BrowserContext.UNTRUSTED_WEB,
            replace(browser.spec, profile_root=browser._profile.path),
        )


@pytest.mark.parametrize("unsafe", ["symlink", "public", "personal"])
def test_profile_authority(tmp_path, session, unsafe):
    root = tmp_path / "profile"
    root.mkdir(mode=0o700)
    if unsafe == "symlink":
        link = tmp_path / "link"
        link.symlink_to(root)
        root = link
    elif unsafe == "public":
        root.chmod(0o755)
    else:
        (root / "Cookies").write_text("private")
    with pytest.raises(ChromiumError, match="invalid_spec"):
        ChromiumController(
            session,
            BrowserContext.TRUSTED_IMAGE,
            ChromiumLaunchSpec(("/trusted/launcher",), root),
        )


def test_crashed_group_leader_does_not_abandon_child(tmp_path):
    from postcardscene.graphics.chromium._launch import OwnedProcess

    child_pid = tmp_path / "child"
    script = (
        "import os, signal, time\n"
        "pid = os.fork()\n"
        "if pid:\n"
        f" open({str(child_pid)!r}, 'w').write(str(pid))\n"
        " os._exit(1)\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n"
    )
    process = OwnedProcess([sys.executable, "-c", script], {})
    try:
        deadline = time.monotonic() + 3
        while not child_pid.exists() or not process.exited():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        pid = int(child_pid.read_text())
        process.stop()
        deadline = time.monotonic() + 1
        while Path(f"/proc/{pid}/stat").exists():
            state = Path(f"/proc/{pid}/stat").read_text().split(") ")[1][0]
            if state == "Z":
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        process.stop()
    finally:
        process.stop()


class FakeCDP:
    def __init__(self, *, load=True):
        self.sent = []
        self.replies = []
        self.load = load
        self.closed = False

    def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        method = message["method"]
        result = {}
        if method == "Browser.getVersion":
            result = {"product": "Chrome/test"}
        elif method == "Target.getTargets":
            from postcardscene.graphics.chromium._launch import BLACK_PAGE

            result = {
                "targetInfos": [{"type": "page", "url": BLACK_PAGE, "targetId": "page"}]
            }
        elif method == "Target.attachToTarget":
            result = {"sessionId": "session"}
        elif method == "Page.navigate":
            result = {"frameId": "frame", "loaderId": str(message["id"])}
            self.frame = {
                "id": "frame",
                "loaderId": str(message["id"]),
                "url": message["params"]["url"],
            }
            if self.load:
                # Load may arrive BEFORE the navigate command response.
                self.replies.append(
                    {
                        "method": "Page.lifecycleEvent",
                        "sessionId": "session",
                        "params": {**result, "name": "load"},
                    }
                )
        elif method == "Page.getFrameTree":
            result = {"frameTree": {"frame": self.frame}}
        self.replies.append(
            {
                "id": message["id"],
                "result": result,
                **({"sessionId": "session"} if "sessionId" in message else {}),
            }
        )

    def recv(self, timeout):
        if self.replies:
            return json.dumps(self.replies.pop(0))
        time.sleep(min(timeout, 0.001))
        raise TimeoutError

    def close(self):
        self.closed = True


@pytest.fixture
def controlled(browser, monkeypatch):
    from postcardscene.graphics.chromium import _cdp

    connections = []

    def connect(*args):
        connection = FakeCDP()
        connections.append(connection)
        return connection

    monkeypatch.setattr(_cdp, "connect_browser", connect)
    return browser, connections


def test_navigation_blank_idempotence_and_restart(controlled):
    browser, connections = controlled
    browser.navigate("http://127.0.0.1:123/frame/token")
    pid = browser._process.pid
    browser.ensure_started()
    assert browser._process.pid == pid and len(connections) == 1
    browser.blank()
    from postcardscene.graphics.chromium._launch import BLACK_PAGE

    navigations = [
        m["params"]["url"]
        for m in connections[0].sent
        if m["method"] == "Page.navigate"
    ]
    assert navigations[-1] == BLACK_PAGE
    browser.restart()
    assert browser._process.pid != pid and connections[0].closed
    browser.navigate("http://127.0.0.1:123/frame/another")
    browser.stop()
    assert connections[1].closed and browser._process is None


def test_command_acceptance_is_not_load_and_timeout_stops_browser(controlled):
    browser, connections = controlled
    browser.ensure_started()
    connections[0].load = False
    with pytest.raises(ChromiumError, match="navigation_timeout"):
        browser.navigate("http://127.0.0.1:123/frame/secret", timeout_seconds=0.05)
    assert connections[0].closed and browser._process is None


def test_cancel_and_browser_crash_are_distinct(controlled):
    browser, _ = controlled
    with pytest.raises(ChromiumError, match="cancelled"):
        browser.navigate("http://127.0.0.1:123/frame/secret", cancelled=lambda: True)
    browser.ensure_started()
    os.kill(browser._process.pid, signal.SIGKILL)
    deadline = time.monotonic() + 1
    while not browser._process.exited():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    with pytest.raises(ChromiumError, match="browser_exited"):
        browser.navigate("http://127.0.0.1:123/frame/secret")


def test_two_launch_specs_share_policy_and_isolate_authority(
    controlled, session, tmp_path, monkeypatch
):
    from postcardscene.graphics.chromium import _launch

    browser, connections = controlled
    root = tmp_path / "web"
    root.mkdir(mode=0o700)
    other = ChromiumController(
        session,
        BrowserContext.UNTRUSTED_WEB,
        ChromiumLaunchSpec(
            ("/usr/bin/env", *browser.spec.command),
            root,
            {"LANG": "en_US.UTF-8"},
        ),
    )
    calls = []
    popen = _launch.subprocess.Popen

    def record(argv, **kwargs):
        calls.append((argv, kwargs))
        return popen(argv, **kwargs)

    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setenv("SECRET_TOKEN", "secret")
    monkeypatch.setenv("POSTCARDSCENE_CONFIG", "/private/admin-config.py")
    monkeypatch.setenv("HOME", "/private/admin-home")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/private/admin-config")
    monkeypatch.setattr(_launch.subprocess, "Popen", record)
    try:
        browser.ensure_started()
        other.navigate("https://example.test/content")
        assert browser._process.pid != other._process.pid
        assert connections[0] is not connections[1]
        for controller, (argv, kwargs) in zip((browser, other), calls, strict=True):
            assert argv[: len(controller.spec.command)] == list(controller.spec.command)
            assert "--ozone-platform=wayland" in argv
            assert "--remote-debugging-address=127.0.0.1" in argv
            assert "--remote-debugging-port=0" in argv
            assert f"--user-data-dir={controller._profile.path}" in argv
            assert not any("no-sandbox" in arg or "allow-file" in arg for arg in argv)
            assert kwargs["shell"] is False and kwargs["start_new_session"] is True
            assert "preexec_fn" not in kwargs
            assert kwargs["env"]["WAYLAND_DISPLAY"] == "wayland-0"
            assert not {"DISPLAY", "SECRET_TOKEN"} & kwargs["env"].keys()
            assert "POSTCARDSCENE_CONFIG" not in kwargs["env"]
            assert kwargs["env"]["HOME"] == str(controller.spec.profile_root)
            assert kwargs["env"]["XDG_CONFIG_HOME"] == str(
                controller.spec.profile_root / "config"
            )
            assert os.getpgid(controller._process.pid) == controller._process.pid
        messages = connections[1].sent
        denied = next(
            i
            for i, message in enumerate(messages)
            if message["method"] == "Browser.setDownloadBehavior"
        )
        navigation = next(
            i
            for i, message in enumerate(messages)
            if message["params"].get("url") == "https://example.test/content"
        )
        assert messages[denied]["params"] == {"behavior": "deny"}
        assert denied < navigation
        with pytest.raises(ChromiumError, match="invalid_url"):
            other.navigate("chrome://settings")
        duplicate = ChromiumController(session, browser.context, browser.spec)
        with pytest.raises(ChromiumError, match="invalid_spec"):
            duplicate.ensure_started()
        assert not connections[0].closed  # A lock contender cannot stop the owner.
    finally:
        other.stop()


@pytest.mark.parametrize(
    "changes",
    [
        {"command": "shell command"},
        {"command": ()},
        {"command": ("relative",)},
        {"command": ("/trusted", "--no-sandbox")},
        {"profile_root": "relative"},
        {"environment": {"DISPLAY": ":0"}},
        {"environment": {"LD_PRELOAD": "secret"}},
    ],
)
def test_invalid_launch_spec(browser, changes):
    with pytest.raises(ChromiumError, match="invalid_spec"):
        replace(browser.spec, **changes)


def test_foreign_profile_and_metadata_are_rejected(browser, session, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(ChromiumError, match="invalid_spec"):
        ChromiumController(session, browser.context, browser.spec)


@pytest.mark.parametrize(
    "raw",
    [
        b"0\n/devtools/browser/id",
        b"65536\n/devtools/browser/id",
        b"123\nws://example.test/control",
        b"x" * 513,
        b"\xff",
    ],
)
def test_malformed_control_metadata_is_bounded_and_redacted(browser, raw):
    from postcardscene.graphics.chromium import _cdp
    from postcardscene.graphics.chromium._errors import Deadline

    browser._profile.metadata.write_bytes(raw)
    with pytest.raises(ChromiumError, match="cdp_startup") as caught:
        _cdp.connect_browser(
            browser._profile,
            SimpleNamespace(exited=lambda: False),
            Deadline(0.05, None),
        )
    assert "example.test" not in str(caught.value)


def test_stale_metadata_is_removed_and_symlinks_are_not_followed(browser, tmp_path):
    browser._profile.metadata.write_text("123\n/devtools/browser/stale")
    with pytest.raises(ChromiumError, match="cdp_startup"):
        browser.ensure_started(timeout_seconds=0.05)
    assert not browser._profile.metadata.exists()
    foreign = tmp_path / "foreign"
    foreign.write_text("secret")
    browser._profile.metadata.symlink_to(foreign)
    with pytest.raises(ChromiumError, match="invalid_spec"):
        browser.ensure_started()
    assert foreign.read_text() == "secret"


@pytest.mark.parametrize(
    "reply",
    [
        "{",
        "[]",
        '"secret"',
        '{"id":999,"result":{}}',
        '{"id":1,"error":{"message":"secret"}}',
        '{"method":"Page.lifecycleEvent","params":[]}',
    ],
)
def test_malformed_protocol_retires_browser(controlled, monkeypatch, reply):
    browser, connections = controlled
    browser.ensure_started()
    monkeypatch.setattr(connections[0], "recv", lambda timeout: reply)
    with pytest.raises(ChromiumError, match="cdp_failed") as caught:
        browser.blank()
    assert "secret" not in str(caught.value)
    assert connections[0].closed and browser._process is None


def test_unexpected_page_target_fails_closed(controlled, monkeypatch):
    browser, connections = controlled
    browser.ensure_started()
    peer = connections[0]
    send = peer.send

    def extra_page(raw):
        send(raw)
        if json.loads(raw)["method"] == "Target.getTargets":
            peer.replies[-1]["result"]["targetInfos"].append(
                {"type": "page", "url": "https://secret.test", "targetId": "other"}
            )

    monkeypatch.setattr(peer, "send", extra_page)
    with pytest.raises(ChromiumError, match="cdp_failed"):
        browser.blank()
    assert peer.closed and browser._process is None


@pytest.mark.parametrize(
    "failure", ["wrong_loader", "download", "error_page", "cancel", "crash"]
)
def test_navigation_failure_signals(controlled, monkeypatch, failure):
    browser, connections = controlled
    browser.ensure_started()
    peer = connections[0]
    send = peer.send
    cancelled = False

    def respond(raw):
        nonlocal cancelled
        send(raw)
        if json.loads(raw)["method"] == "Page.navigate":
            if failure == "wrong_loader":
                peer.replies[-2]["params"]["loaderId"] = "old-loader"
            elif failure == "download":
                peer.replies[-1]["result"]["isDownload"] = True
            elif failure == "error_page":
                peer.frame["unreachableUrl"] = "https://secret.test"
            elif failure == "cancel":
                cancelled = True
            else:
                os.kill(browser._process.pid, signal.SIGKILL)
                deadline = time.monotonic() + 1
                while not browser._process.exited():
                    assert time.monotonic() < deadline
                    time.sleep(0.001)

    monkeypatch.setattr(peer, "send", respond)
    expected = {
        "wrong_loader": "navigation_timeout",
        "cancel": "cancelled",
        "crash": "browser_exited",
    }.get(failure, "navigation_failed")
    with pytest.raises(ChromiumError, match=expected):
        browser.navigate(
            "http://127.0.0.1:123/frame/secret",
            cancelled=lambda: cancelled,
            timeout_seconds=0.1,
        )
    assert peer.closed and browser._process is None


def test_cleanup_failure_preserves_primary_and_process_authority(
    controlled, monkeypatch
):
    from postcardscene.graphics.chromium import Failure

    browser, connections = controlled
    browser.ensure_started()
    connections[0].load = False

    def failed_cleanup():
        raise ChromiumError(Failure.CLEANUP_FAILED)

    with monkeypatch.context() as patch:
        patch.setattr(browser._process, "stop", failed_cleanup)
        with pytest.raises(ChromiumError, match="navigation_timeout") as caught:
            browser.blank(timeout_seconds=0.05)
        assert caught.value.cleanup_failed
        assert browser._process is not None and browser._profile._lock is not None
        retained = browser._process
        with pytest.raises(ChromiumError, match="cleanup_failed"):
            browser.ensure_started()
        assert browser._process is retained
    browser.stop()


def test_lost_session_retires_healthy_browser(controlled, session):
    browser, connections = controlled
    browser.ensure_started()
    session.inspect = lambda: SimpleNamespace(available=False)
    with pytest.raises(ChromiumError, match="session_unavailable"):
        browser.ensure_started()
    assert connections[0].closed and browser._process is None


def test_startup_waits_for_the_initial_page(controlled, monkeypatch):
    send = FakeCDP.send
    first = True

    def starting(self, raw):
        nonlocal first
        send(self, raw)
        if json.loads(raw)["method"] == "Target.getTargets" and first:
            self.replies[-1]["result"]["targetInfos"] = []
            first = False

    monkeypatch.setattr(FakeCDP, "send", starting)
    controlled[0].ensure_started()
    assert not first


def test_same_document_navigation_requires_matching_event_and_frame(
    controlled, monkeypatch
):
    browser, connections = controlled
    browser.ensure_started()
    peer = connections[0]
    send = peer.send

    def same_document(raw):
        send(raw)
        if json.loads(raw)["method"] == "Page.navigate":
            peer.replies[-1]["result"].pop("loaderId")
            peer.replies[-2] = {
                "sessionId": "session",
                "method": "Page.navigatedWithinDocument",
                "params": {"frameId": "frame", "url": peer.frame["url"]},
            }

    monkeypatch.setattr(peer, "send", same_document)
    browser.navigate("http://127.0.0.1:123/frame/same")


@pytest.mark.parametrize("response", ["normal", "oversized", "eof"])
def test_real_loopback_websocket_is_private_bounded_and_survives_idle(
    browser, monkeypatch, response
):
    from websockets.sync.server import serve

    from postcardscene.graphics.chromium import _cdp
    from postcardscene.graphics.chromium._errors import Deadline

    def handler(socket):
        if response == "normal":
            # Longer than the startup I/O slice: socket receive must not retain
            # the TCP connect timeout after the handshake.
            time.sleep(0.1)
            socket.send('{"id":1,"result":{"product":"Chrome/test"}}')
        elif response == "oversized":
            socket.send("x" * (_cdp.MESSAGE_LIMIT + 1))
        else:
            socket.close()

    monkeypatch.setenv("http_proxy", "http://unrelated.invalid:9999")
    with serve(
        handler, "127.0.0.1", 0, close_timeout=0.1, logger=_cdp._LOGGER
    ) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        port = server.socket.getsockname()[1]
        browser._profile.metadata.write_text(f"{port}\n/devtools/browser/test")
        process = SimpleNamespace(exited=lambda: False)
        connection = _cdp.connect_browser(browser._profile, process, Deadline(2, None))
        try:
            assert connection.socket.getpeername() == ("127.0.0.1", port)
            control = _cdp.PageControl(connection, process)
            if response == "normal":
                assert (
                    control._receive(Deadline(1, None), _cdp.Failure.CDP_FAILED)[
                        "result"
                    ]["product"]
                    == "Chrome/test"
                )
            else:
                with pytest.raises(ChromiumError, match="cdp_failed"):
                    control._receive(Deadline(1, None), _cdp.Failure.CDP_FAILED)
        finally:
            connection.close()
            server.shutdown()
            thread.join(2)
