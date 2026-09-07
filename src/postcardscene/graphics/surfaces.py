"""Serialized content-class ownership, without Scene/Sequence decisions."""

from contextlib import contextmanager
from enum import StrEnum

from ._capability import CapabilityError, CapabilityStatus
from .chromium import BrowserContext, ChromiumError


class ContentClass(StrEnum):
    NONE = "none"
    TRUSTED_IMAGE = "trusted_image"
    UNTRUSTED_WEB = "untrusted_web"
    VIDEO = "video"


class ContentSurfaces:
    """Sole owner of supplied, initially stopped controllers on one session.

    Callers use select/stop or operation; they must not independently start/stop
    these controllers outside this coordinator.
    The runtime serializes calls and invokes reconcile during health polling.
    A stop failure retains authority and prevents all replacement activation.
    """

    def __init__(self, trusted, untrusted, video):
        if (
            trusted.context != BrowserContext.TRUSTED_IMAGE
            or untrusted.context != BrowserContext.UNTRUSTED_WEB
            or trusted.session != untrusted.session
            or trusted.session != video.session
        ):
            raise CapabilityError("invalid_spec")
        self._owners = {
            ContentClass.TRUSTED_IMAGE: trusted,
            ContentClass.UNTRUSTED_WEB: untrusted,
            ContentClass.VIDEO: video,
        }
        # Establish the initial invariant even if supplied controllers were used.
        for owner in self._owners.values():
            owner.stop()
        self.active = ContentClass.NONE
        self._retiring = False
        self.reason = "ready"
        self.capabilities = {
            content: CapabilityStatus(False, "not_probed") for content in self._owners
        }

    def select(self, content, *, timeout_seconds=10, cancelled=None):
        if not isinstance(content, ContentClass):
            raise CapabilityError("invalid_spec")
        if self._retiring or content != self.active:
            self.stop()
        if content == ContentClass.NONE:
            return
        owner = self._owners[content]
        self.active = content  # Retain cleanup authority even during failed start.
        try:
            owner.ensure_started(timeout_seconds=timeout_seconds, cancelled=cancelled)
            self.reason = "ready"
            self.capabilities[content] = CapabilityStatus(True, "ready")
        except (ChromiumError, CapabilityError):
            error = CapabilityError("content_unavailable")
            self.capabilities[content] = CapabilityStatus(False, error.reason)
            try:
                self.stop()
            except CapabilityError:
                error.cleanup_failed = True
            self.reason = error.reason
            raise error from None

    @contextmanager
    def operation(self, content):
        """Lend the exact owner for a serialized content operation.

        This does not switch content classes or start a controller. The caller
        may use its bounded content methods; this coordinator retains retirement
        authority on failure. Global switching still uses select/stop.
        """
        if not isinstance(content, ContentClass) or content == ContentClass.NONE:
            raise CapabilityError("invalid_spec")
        if self._retiring:
            raise CapabilityError("cleanup_failed")
        if self.active not in (ContentClass.NONE, content):
            raise CapabilityError("content_busy")
        self.active = content
        try:
            yield self._owners[content]
        except (ChromiumError, CapabilityError) as error:
            try:
                self.stop()
            except CapabilityError:
                error.cleanup_failed = True
            raise

    def reconcile(self, **kwargs):
        """Recheck existing owner, retire crashes to black; no automatic retry."""
        self.select(self.active, **kwargs)

    def stop(self):
        if self.active == ContentClass.NONE:
            return
        self._retiring = True
        try:
            self._owners[self.active].stop()
        except (ChromiumError, CapabilityError):
            self.reason = "cleanup_failed"
            raise CapabilityError("cleanup_failed") from None
        self.active = ContentClass.NONE
        self._retiring = False
        self.reason = "ready"

    def public_diagnostics(self):
        return {
            "active_content": self.active.value,
            "reason": self.reason,
            "capabilities": {
                content.value: status.public_diagnostics()
                for content, status in self.capabilities.items()
            },
        }
