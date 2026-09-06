"""Controller contracts with an owned fake browser and private CDP peer."""

import json
import os
import signal
import sys
import time
from pathlib import Path
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
