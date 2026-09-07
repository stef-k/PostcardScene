"""Synchronous web presentation beneath the shared content-surface owner."""

from enum import StrEnum

from postcardscene.graphics._capability import CapabilityError
from postcardscene.graphics.chromium import ChromiumError, Failure
from postcardscene.graphics.surfaces import ContentClass, ContentSurfaces
from postcardscene.web_selection import (
    WebSelectionError,
    WebTarget,
    validate_web_source,
)


class WebFailure(StrEnum):
    INVALID_TARGET = "invalid_target"
    UNAVAILABLE = "unavailable"
    CANCELLED = "cancelled"
    CLEANUP_FAILED = "cleanup_failed"


class WebRendererError(Exception):
    """Only fixed reasons and secondary cleanup status are public."""

    def __init__(self, reason: WebFailure, *, cleanup_failed=False):
        self.reason = reason
        self.cleanup_failed = cleanup_failed
        super().__init__(reason.value)


def _failure(error):
    reason = WebFailure.UNAVAILABLE
    if error.reason == "cancelled":
        reason = WebFailure.CANCELLED
    elif error.reason == "cleanup_failed":
        reason = WebFailure.CLEANUP_FAILED
    return WebRendererError(reason, cleanup_failed=error.cleanup_failed)


class WebRenderer:
    """Caller serializes operations outside Flask and database transactions.

    Other active content must first be retired by the global runtime owner.
    No target, URL or claimed loaded-page state is retained by this capability.
    """

    def __init__(self, surfaces: ContentSurfaces):
        self._surfaces = surfaces

    def show(self, target: WebTarget, *, cancelled=None):
        try:
            if (
                type(target) is not WebTarget
                or type(target.source_id) is not int
                or target.source_id <= 0
            ):
                raise WebSelectionError()
            configuration = validate_web_source("web_url", {"url": target.url})
            if configuration["url"] != target.url:
                raise WebSelectionError()
        except WebSelectionError:
            error = WebRendererError(WebFailure.INVALID_TARGET)
            try:
                self.stop()
            except WebRendererError:
                error.cleanup_failed = True
            raise error from None

        for attempt in range(2):
            try:
                with self._surfaces.operation(ContentClass.UNTRUSTED_WEB) as browser:
                    # Retire stale remote content before every fresh presentation.
                    browser.blank(cancelled=cancelled)
                    browser.navigate(
                        target.url, timeout_seconds=15, cancelled=cancelled
                    )
                return
            except ChromiumError as error:
                if (
                    attempt == 0
                    and not error.cleanup_failed
                    and error.reason
                    in {
                        Failure.STARTUP_FAILED,
                        Failure.CDP_STARTUP,
                        Failure.CDP_FAILED,
                        Failure.NAVIGATION_TIMEOUT,
                        Failure.NAVIGATION_FAILED,
                        Failure.BROWSER_EXITED,
                    }
                ):
                    continue
                raise _failure(error) from None
            except CapabilityError as error:
                raise _failure(error) from None

    def clear(self, *, cancelled=None):
        if self._surfaces.active != ContentClass.UNTRUSTED_WEB:
            return
        try:
            with self._surfaces.operation(ContentClass.UNTRUSTED_WEB) as browser:
                browser.blank(cancelled=cancelled)
        except (ChromiumError, CapabilityError) as error:
            raise _failure(error) from None

    def stop(self):
        if self._surfaces.active != ContentClass.UNTRUSTED_WEB:
            return
        try:
            self._surfaces.stop()
        except CapabilityError as error:
            raise _failure(error) from None
