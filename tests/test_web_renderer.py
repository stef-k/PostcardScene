"""Web presentation policy through real surface and Chromium navigation seams."""

from types import SimpleNamespace

import pytest

from postcardscene.graphics.chromium import (
    BrowserContext,
    ChromiumController,
    ChromiumError,
    Failure,
)
from postcardscene.graphics.chromium._launch import BLACK_PAGE
from postcardscene.graphics.surfaces import ContentClass, ContentSurfaces
from postcardscene.web_renderer import WebFailure, WebRenderer, WebRendererError
from postcardscene.web_selection import WebTarget


@pytest.fixture
def rendering(monkeypatch):
    calls = []
    failures = []
    session = object()
    browser = object.__new__(ChromiumController)
    browser.context = BrowserContext.UNTRUSTED_WEB
    browser.session = session
    browser.running = False
    browser.stop_fails = False

    def start(self, deadline):
        deadline.check(Failure.CDP_STARTUP)
        if not self.running:
            calls.append("start")
            self.running = True

    def navigate(url, deadline):
        deadline.check(Failure.NAVIGATION_TIMEOUT)
        calls.append(url)
        if failures:
            failure = failures.pop(0)
            if failure:
                raise ChromiumError(failure)
        return url

    def stop(self):
        calls.append("stop")
        if self.stop_fails:
            raise ChromiumError(Failure.CLEANUP_FAILED)
        self.running = False

    monkeypatch.setattr(ChromiumController, "_start", start)
    monkeypatch.setattr(ChromiumController, "stop", stop)
    browser._control = SimpleNamespace(navigate=navigate)
    trusted = SimpleNamespace(
        context=BrowserContext.TRUSTED_IMAGE,
        session=session,
        stop=lambda: calls.append("trusted_stop"),
    )
    video = SimpleNamespace(session=session, stop=lambda: calls.append("video_stop"))
    surfaces = ContentSurfaces(trusted, browser, video)
    calls.clear()
    return WebRenderer(surfaces), surfaces, browser, calls, failures


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/share?secret=value",
        "http://192.168.1.2/view",
        "http://localhost:8080/",
    ],
)
def test_each_presentation_loads_black_then_fresh_target(rendering, url):
    renderer, surfaces, browser, calls, _ = rendering
    target = WebTarget(1, url)
    renderer.show(target)
    renderer.show(target)
    assert calls == ["start", BLACK_PAGE, url, BLACK_PAGE, url]
    assert browser.running
    assert surfaces.active == ContentClass.UNTRUSTED_WEB
    assert url not in repr(target)
    renderer.clear()
    assert calls[-1] == BLACK_PAGE
    renderer.stop()
    renderer.stop()
    assert calls[-1] == "stop"
    assert not browser.running
    assert surfaces.active == ContentClass.NONE


@pytest.mark.parametrize(
    "failure",
    [
        Failure.STARTUP_FAILED,
        Failure.CDP_STARTUP,
        Failure.CDP_FAILED,
        Failure.NAVIGATION_TIMEOUT,
        Failure.NAVIGATION_FAILED,
        Failure.BROWSER_EXITED,
    ],
)
def test_recoverable_failure_has_one_fresh_browser_retry(rendering, failure):
    renderer, _, browser, calls, failures = rendering
    failures.extend([None, failure])
    renderer.show(WebTarget(1, "https://example.org/"))
    assert calls.count("start") == 2
    assert calls.count("https://example.org/") == 2
    assert calls.index("stop") < calls.index("start", 1)
    assert browser.running


def test_final_failure_is_sanitized_retired_and_later_show_recovers(rendering):
    renderer, surfaces, browser, calls, failures = rendering
    url = "https://example.org/private?token=secret"
    failures.extend([None, Failure.NAVIGATION_FAILED, None, Failure.NAVIGATION_TIMEOUT])
    with pytest.raises(WebRendererError) as caught:
        renderer.show(WebTarget(1, url))
    assert caught.value.reason == WebFailure.UNAVAILABLE
    assert url not in str(caught.value)
    assert not caught.value.cleanup_failed
    assert calls.count("start") == 2
    assert not browser.running
    assert surfaces.active == ContentClass.NONE
    renderer.show(WebTarget(1, url))
    assert browser.running


@pytest.mark.parametrize(
    "target",
    [
        None,
        "https://example.org/",
        WebTarget(0, "https://example.org/"),
        WebTarget(1, "file:///secret"),
        WebTarget(1, " https://example.org/"),
    ],
)
def test_invalid_replacement_retires_previous_page_without_navigation(
    rendering, target
):
    renderer, surfaces, browser, calls, _ = rendering
    renderer.show(WebTarget(1, "https://example.org/"))
    calls.clear()
    with pytest.raises(WebRendererError, match="invalid_target"):
        renderer.show(target)
    assert calls == ["stop"]
    assert not browser.running
    assert surfaces.active == ContentClass.NONE


def test_cancellation_is_not_retried_and_retires_previous_page(rendering):
    renderer, surfaces, browser, calls, _ = rendering
    renderer.show(WebTarget(1, "https://example.org/"))
    calls.clear()
    with pytest.raises(WebRendererError, match="cancelled"):
        renderer.show(WebTarget(1, "https://example.org/next"), cancelled=lambda: True)
    assert "start" not in calls
    assert not browser.running
    assert surfaces.active == ContentClass.NONE


@pytest.mark.parametrize("operation", ["show", "clear"])
def test_blanking_failure_retires_and_cleanup_uncertainty_blocks_retry(
    rendering, operation
):
    renderer, surfaces, browser, calls, failures = rendering
    target = WebTarget(1, "https://example.org/")
    renderer.show(target)
    calls.clear()
    failures.append(Failure.NAVIGATION_FAILED)
    browser.stop_fails = True
    with pytest.raises(WebRendererError) as caught:
        getattr(renderer, operation)(*([target] if operation == "show" else []))
    assert caught.value.cleanup_failed
    assert "start" not in calls
    assert surfaces.active == ContentClass.UNTRUSTED_WEB
    with pytest.raises(WebRendererError, match="cleanup_failed"):
        renderer.show(target)
    browser.stop_fails = False
    renderer.stop()
    assert surfaces.active == ContentClass.NONE


def test_clear_failure_without_cleanup_failure_retires_to_black(rendering):
    renderer, surfaces, browser, _, failures = rendering
    renderer.show(WebTarget(1, "https://example.org/"))
    failures.append(Failure.NAVIGATION_FAILED)
    with pytest.raises(WebRendererError, match="unavailable"):
        renderer.clear()
    assert not browser.running
    assert surfaces.active == ContentClass.NONE


def test_renderer_cannot_switch_or_retire_another_content_class(rendering):
    renderer, surfaces, _, calls, _ = rendering
    surfaces.active = ContentClass.TRUSTED_IMAGE
    with pytest.raises(WebRendererError, match="unavailable"):
        renderer.show(WebTarget(1, "https://example.org/"))
    renderer.clear()
    renderer.stop()
    assert calls == []
    assert surfaces.active == ContentClass.TRUSTED_IMAGE
