"""Safe Chromium failure vocabulary and cooperative operation deadlines."""

import math
import time
from collections.abc import Callable
from enum import StrEnum


class Failure(StrEnum):
    INVALID_SPEC = "invalid_spec"
    INVALID_URL = "invalid_url"
    SESSION_UNAVAILABLE = "session_unavailable"
    STARTUP_FAILED = "startup_failed"
    CDP_STARTUP = "cdp_startup"
    CDP_FAILED = "cdp_failed"
    NAVIGATION_TIMEOUT = "navigation_timeout"
    NAVIGATION_FAILED = "navigation_failed"
    BROWSER_EXITED = "browser_exited"
    CANCELLED = "cancelled"
    CLEANUP_FAILED = "cleanup_failed"


class ChromiumError(Exception):
    """Public text contains only fixed vocabulary, even when cleanup also fails."""

    def __init__(self, reason: Failure, *, cleanup_failed: bool = False):
        self.reason = reason
        self.cleanup_failed = cleanup_failed
        super().__init__(reason.value)


class Deadline:
    def __init__(self, seconds: float, cancelled: Callable[[], bool] | None):
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (float, int))
            or not math.isfinite(seconds)
            or not 0 < seconds <= 120
        ):
            raise ChromiumError(Failure.INVALID_SPEC)
        self.end = time.monotonic() + seconds
        self.cancelled = cancelled

    def check(self, failure: Failure) -> float:
        if self.cancelled is not None and self.cancelled():
            raise ChromiumError(Failure.CANCELLED)
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise ChromiumError(failure)
        return min(0.05, remaining)
