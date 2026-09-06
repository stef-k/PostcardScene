"""One-owner lifecycle using existing Chromium controller seams."""

from types import SimpleNamespace

import pytest

from postcardscene.graphics._capability import CapabilityError
from postcardscene.graphics.chromium import BrowserContext, ChromiumController
from postcardscene.graphics.surfaces import ContentClass, ContentSurfaces


def owners(monkeypatch):
    calls = []
    session = object()
    # Use real controller methods at the surface seam; #64 owns CDP proof.
    trusted = object.__new__(ChromiumController)
    untrusted = object.__new__(ChromiumController)
    for owner, context in (
        (trusted, BrowserContext.TRUSTED_IMAGE),
        (untrusted, BrowserContext.UNTRUSTED_WEB),
    ):
        owner.session = session
        owner.context = context
    monkeypatch.setattr(
        ChromiumController,
        "stop",
        lambda self: calls.append((self.context.value, "stop")),
    )
    monkeypatch.setattr(
        ChromiumController,
        "ensure_started",
        lambda self, **kw: calls.append((self.context.value, "start")),
    )
    video = SimpleNamespace(
        session=session,
        stop=lambda: calls.append(("video", "stop")),
        ensure_started=lambda **kw: calls.append(("video", "start")),
    )
    surfaces = ContentSurfaces(trusted, untrusted, video)
    calls.clear()
    return surfaces, calls, video


def test_switches_retire_before_replacement_and_reuse_same_class(monkeypatch):
    surfaces, calls, _ = owners(monkeypatch)
    for content in (
        ContentClass.TRUSTED_IMAGE,
        ContentClass.UNTRUSTED_WEB,
        ContentClass.VIDEO,
        ContentClass.NONE,
    ):
        surfaces.select(content)
    assert calls == [
        ("trusted_image", "start"),
        ("trusted_image", "stop"),
        ("untrusted_web", "start"),
        ("untrusted_web", "stop"),
        ("video", "start"),
        ("video", "stop"),
    ]
    assert surfaces.public_diagnostics() == {
        "active_content": "none",
        "reason": "ready",
    }
    surfaces.select(ContentClass.TRUSTED_IMAGE)
    calls.clear()
    surfaces.reconcile()
    assert calls == [("trusted_image", "start")]


def test_cleanup_failure_blocks_replacement_and_retains_authority(monkeypatch):
    surfaces, calls, video = owners(monkeypatch)
    surfaces.select(ContentClass.VIDEO)

    def fail():
        raise CapabilityError("cleanup_failed")

    video.stop = fail
    with pytest.raises(CapabilityError, match="cleanup_failed"):
        surfaces.select(ContentClass.TRUSTED_IMAGE)
    assert surfaces.active == ContentClass.VIDEO
    assert ("trusted_image", "start") not in calls
    video.stop = lambda: calls.append(("video", "stop"))
    surfaces.select(ContentClass.TRUSTED_IMAGE)
    assert calls[-2:] == [("video", "stop"), ("trusted_image", "start")]


def test_crash_converges_to_none_without_retry_or_old_surface(monkeypatch):
    surfaces, calls, video = owners(monkeypatch)
    surfaces.select(ContentClass.TRUSTED_IMAGE)
    surfaces.select(ContentClass.VIDEO)

    def failed(**kwargs):
        raise CapabilityError("helper_exited")

    video.ensure_started = failed
    with pytest.raises(CapabilityError, match="content_unavailable"):
        surfaces.reconcile()
    assert surfaces.active == ContentClass.NONE
    assert calls[-1] == ("video", "stop")
    before = calls.copy()
    surfaces.reconcile()
    assert calls == before
