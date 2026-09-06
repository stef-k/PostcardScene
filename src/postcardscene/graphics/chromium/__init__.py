"""One synchronous Chromium controller, independent of content/runtime/Flask.

The runtime owner serializes calls. Cancellation predicates may observe its
existing shutdown Event; no controller thread, worker or retry loop is started.
"""

from collections.abc import Callable
from urllib.parse import urlsplit

from postcardscene.graphics import WaylandSession

from . import _cdp
from ._errors import ChromiumError, Deadline, Failure
from ._launch import (
    BLACK_PAGE,
    BrowserContext,
    ChromiumLaunchSpec,
    OwnedProcess,
    Profile,
    launch_arguments,
)

__all__ = [
    "BrowserContext",
    "ChromiumController",
    "ChromiumError",
    "ChromiumLaunchSpec",
    "Failure",
]


def _validate_url(url: str, context: BrowserContext):
    try:
        if not isinstance(url, str) or not 0 < len(url) <= 8192:
            raise ValueError
        if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
            raise ValueError
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError
        if parsed.port is not None and parsed.port == 0:
            raise ValueError
        if context == BrowserContext.TRUSTED_IMAGE and (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or not parsed.path.startswith("/")
            or parsed.fragment
            or "#" in url
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise ChromiumError(Failure.INVALID_URL) from None


class ChromiumController:
    def __init__(
        self, session: WaylandSession, context: BrowserContext, spec: ChromiumLaunchSpec
    ):
        if not isinstance(context, BrowserContext) or not isinstance(
            spec, ChromiumLaunchSpec
        ):
            raise ChromiumError(Failure.INVALID_SPEC)
        self.session = session
        self.context = context
        self.spec = spec
        self._profile = Profile(spec.profile_root, context)
        self._process = None
        self._control = None

    def ensure_started(
        self,
        *,
        cancelled: Callable[[], bool] | None = None,
        timeout_seconds: float = 10,
    ):
        """Browser/control readiness only; no physical display or image decoding."""
        deadline = Deadline(timeout_seconds, cancelled)
        try:
            self._start(deadline)
        except ChromiumError as error:
            self._retire(error)

    def _start(self, deadline: Deadline):
        deadline.check(Failure.CDP_STARTUP)
        if self._process is not None:
            if self._process.exited():
                raise ChromiumError(Failure.BROWSER_EXITED)
            if self._control is not None:
                self._control._call(
                    "Browser.getVersion", {}, deadline, Failure.CDP_FAILED, browser=True
                )
                return
        if not self.session.inspect().available:
            raise ChromiumError(Failure.SESSION_UNAVAILABLE)
        try:
            environment = self.session.client_environment()
        except (OSError, ValueError):
            raise ChromiumError(Failure.SESSION_UNAVAILABLE) from None
        self._profile.acquire()
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            **self.spec.environment,
            "HOME": str(self.spec.profile_root),
            "XDG_CONFIG_HOME": str(self.spec.profile_root / "config"),
            "XDG_CACHE_HOME": str(self.spec.profile_root / "cache"),
            **environment,
        }
        self._process = OwnedProcess(
            launch_arguments(self.spec, self._profile), environment
        )
        connection = _cdp.connect_browser(self._profile, self._process, deadline)
        self._control = _cdp.PageControl(connection, self._process)
        self._control.attach(deadline)
        self._control.navigate(BLACK_PAGE, deadline)

    def navigate(
        self,
        url: str,
        *,
        cancelled: Callable[[], bool] | None = None,
        timeout_seconds: float = 15,
    ):
        _validate_url(url, self.context)  # Invalid caller input never reaches Chromium.
        self._navigate(url, Deadline(timeout_seconds, cancelled))

    def blank(
        self, *, cancelled: Callable[[], bool] | None = None, timeout_seconds: float = 5
    ):
        self._navigate(BLACK_PAGE, Deadline(timeout_seconds, cancelled))

    def _navigate(self, url, deadline):
        try:
            self._start(deadline)
            final_url = self._control.navigate(url, deadline)
            if url != BLACK_PAGE:
                try:
                    _validate_url(final_url, self.context)
                except ChromiumError:
                    raise ChromiumError(Failure.NAVIGATION_FAILED) from None
        except ChromiumError as error:
            # Failed/hung/cancelled content cannot remain visible as safe state.
            # Retire the whole browser, exposing labwc's black background.
            self._retire(error)

    def restart(
        self,
        *,
        cancelled: Callable[[], bool] | None = None,
        timeout_seconds: float = 10,
    ):
        self.stop()
        self.ensure_started(cancelled=cancelled, timeout_seconds=timeout_seconds)

    def stop(self):
        """Close CDP and TERM/KILL the pinned group, retaining failed authority."""
        failed = False
        if self._control is not None:
            try:
                self._control.close()
            except ChromiumError:
                failed = True
            self._control = None
        if self._process is not None:
            try:
                self._process.stop()
            except ChromiumError:
                raise ChromiumError(Failure.CLEANUP_FAILED) from None
            self._process = None
        try:
            if self._profile._lock is not None:
                self._profile.clear_metadata()
        except (OSError, ChromiumError):
            failed = True
        finally:
            self._profile.release()
        if failed:
            raise ChromiumError(Failure.CLEANUP_FAILED)

    def _retire(self, error: ChromiumError):
        try:
            self.stop()
        except ChromiumError:
            error.cleanup_failed = True
        raise error from None
