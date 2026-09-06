"""Private, bounded CDP subset for one browser and one flattened page session."""

import json
import logging
import re
import socket
import struct
import time

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from ._errors import ChromiumError, Deadline, Failure
from ._launch import BLACK_PAGE, OwnedProcess, Profile, owned_file

MESSAGE_LIMIT = 256 * 1024
_LOGGER = logging.Logger("postcardscene.private_cdp")
_LOGGER.addHandler(logging.NullHandler())
_LOGGER.propagate = False


def connect_browser(profile: Profile, process: OwnedProcess, deadline: Deadline):
    """Read only fresh browser-owned metadata; never discover via a public URL."""
    while True:
        pause = deadline.check(Failure.CDP_STARTUP)
        if process.exited():
            raise ChromiumError(Failure.BROWSER_EXITED)
        try:
            raw = owned_file(profile.metadata, 512).decode("ascii")
        except FileNotFoundError:
            time.sleep(pause)
            continue
        except (OSError, ValueError, ChromiumError):
            raise ChromiumError(Failure.CDP_STARTUP) from None
        match = re.fullmatch(
            r"([0-9]{1,5})\n(/devtools/browser/[a-zA-Z0-9-]{1,128})\n?", raw
        )
        if match is None or not 1 <= int(match[1]) <= 65535:
            # Chromium creates then writes this file; never accept a partial
            # record, but allow it to complete within the startup deadline.
            time.sleep(pause)
            continue
        # Construct, rather than trust, endpoint host/scheme. No HTTP discovery,
        # redirect, DNS, proxy or inherited proxy environment participates.
        endpoint = f"ws://127.0.0.1:{match[1]}{match[2]}"
        transport = None
        try:
            pause = deadline.check(Failure.CDP_STARTUP, interval=0.25)
            transport = socket.create_connection(
                ("127.0.0.1", int(match[1])), timeout=pause
            )
            # websockets bounds receives/messages/queues, but sendall uses a
            # blocking socket. Bound kernel writes without disrupting its reader.
            transport.setsockopt(
                socket.SOL_SOCKET, socket.SO_SNDTIMEO, struct.pack("ll", 0, 50000)
            )
            transport.settimeout(None)  # The library owns receive deadlines.
            return connect(
                endpoint,
                sock=transport,
                proxy=None,
                open_timeout=deadline.check(Failure.CDP_STARTUP, interval=0.25),
                close_timeout=0.1,
                compression=None,
                ping_interval=None,
                max_size=MESSAGE_LIMIT,
                max_queue=8,
                logger=_LOGGER,
            )
        except (OSError, WebSocketException, TimeoutError):
            if transport is not None:
                transport.close()
            raise ChromiumError(Failure.CDP_STARTUP) from None
        except ChromiumError:
            if transport is not None:
                transport.close()
            raise


def _identifier(value) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= 256:
        raise ChromiumError(Failure.CDP_FAILED)
    return value


class PageControl:
    def __init__(self, connection, process: OwnedProcess):
        self.connection = connection
        self.process = process
        self._sequence = 0
        self._session = None
        self._target = None

    def attach(self, deadline: Deadline):
        version, _ = self._call("Browser.getVersion", {}, deadline, Failure.CDP_STARTUP)
        _identifier(version.get("product"))
        while True:
            pages = self._pages(deadline, Failure.CDP_STARTUP)
            if len(pages) == 1 and pages[0].get("url") == BLACK_PAGE:
                break
            if len(pages) > 1:
                raise ChromiumError(Failure.CDP_FAILED)
            time.sleep(deadline.check(Failure.CDP_STARTUP))
        self._target = _identifier(pages[0].get("targetId"))
        result, _ = self._call(
            "Target.attachToTarget",
            {"targetId": self._target, "flatten": True},
            deadline,
            Failure.CDP_STARTUP,
        )
        self._session = _identifier(result.get("sessionId"))
        self._call("Page.enable", {}, deadline, Failure.CDP_STARTUP)
        self._call(
            "Page.setLifecycleEventsEnabled",
            {"enabled": True},
            deadline,
            Failure.CDP_STARTUP,
        )
        # Browser policy, not arbitrary caller CDP: downloads must not become
        # invisible filesystem writes when a requested navigation is an attachment.
        self._call(
            "Browser.setDownloadBehavior",
            {"behavior": "deny"},
            deadline,
            Failure.CDP_STARTUP,
            browser=True,
        )

    def _pages(self, deadline, failure):
        result, _ = self._call("Target.getTargets", {}, deadline, failure, browser=True)
        targets = result.get("targetInfos")
        if (
            not isinstance(targets, list)
            or len(targets) > 64
            or any(not isinstance(target, dict) for target in targets)
        ):
            raise ChromiumError(Failure.CDP_FAILED)
        return [target for target in targets if target.get("type") == "page"]

    def check(self, deadline, failure=Failure.CDP_FAILED):
        pages = self._pages(deadline, failure)
        if len(pages) != 1 or pages[0].get("targetId") != self._target:
            raise ChromiumError(Failure.CDP_FAILED)

    def _check(self, deadline: Deadline, failure: Failure) -> float:
        if self.process.exited():
            raise ChromiumError(Failure.BROWSER_EXITED)
        return deadline.check(failure)

    def _receive(self, deadline: Deadline, failure: Failure) -> dict:
        while True:
            pause = self._check(deadline, failure)
            try:
                raw = self.connection.recv(timeout=pause)
            except TimeoutError:
                continue
            except (OSError, WebSocketException):
                self._check(deadline, failure)
                raise ChromiumError(Failure.CDP_FAILED) from None
            try:
                if not isinstance(raw, str) or len(raw) > MESSAGE_LIMIT:
                    raise ValueError
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError
                if "id" not in message and (
                    not isinstance(message.get("method"), str)
                    or not isinstance(message.get("params", {}), dict)
                ):
                    raise ValueError
            except (ValueError, TypeError, RecursionError):
                raise ChromiumError(Failure.CDP_FAILED) from None
            method = message.get("method")
            if method in (
                "Inspector.targetCrashed",
                "Inspector.detached",
                "Target.detachedFromTarget",
            ):
                raise ChromiumError(Failure.CDP_FAILED)
            return message

    def _call(self, method, params, deadline, failure, *, browser=False):
        self._check(deadline, failure)
        self._sequence += 1
        message = {"id": self._sequence, "method": method, "params": params}
        session = None if browser else self._session
        if session is not None:
            message["sessionId"] = session
        try:
            self.connection.send(json.dumps(message))
        except (OSError, WebSocketException):
            self._check(deadline, failure)
            raise ChromiumError(Failure.CDP_FAILED) from None
        events = []
        while True:
            reply = self._receive(deadline, failure)
            if "id" in reply:
                if (
                    type(reply["id"]) is not int
                    or reply["id"] != self._sequence
                    or reply.get("sessionId") != session
                ):
                    raise ChromiumError(Failure.CDP_FAILED)
                if "error" in reply or not isinstance(reply.get("result"), dict):
                    raise ChromiumError(Failure.CDP_FAILED)
                return reply["result"], events
            if reply.get("sessionId") == self._session:
                if len(events) >= 128:
                    raise ChromiumError(Failure.CDP_FAILED)
                events.append(reply)

    def navigate(self, url: str, deadline: Deadline) -> str:
        failure = Failure.NAVIGATION_TIMEOUT
        result, events = self._call("Page.navigate", {"url": url}, deadline, failure)
        if result.get("errorText") or result.get("isDownload"):
            raise ChromiumError(Failure.NAVIGATION_FAILED)
        frame = _identifier(result.get("frameId"))
        loader = result.get("loaderId")
        if loader is not None:
            _identifier(loader)
        while True:
            self._check(deadline, failure)
            event = events.pop(0) if events else self._receive(deadline, failure)
            if "id" in event:
                raise ChromiumError(Failure.CDP_FAILED)
            if event.get("sessionId") != self._session:
                continue
            params = event.get("params", {})
            if self._loaded(event.get("method"), params, frame, loader, url):
                break
        # Verify that load belongs to the still-active main document; browser
        # error pages and a later competing navigation aren't successful content.
        result, _ = self._call("Page.getFrameTree", {}, deadline, failure)
        tree = result.get("frameTree")
        current = tree.get("frame") if isinstance(tree, dict) else None
        if (
            not isinstance(current, dict)
            or current.get("id") != frame
            or current.get("unreachableUrl")
        ):
            raise ChromiumError(Failure.NAVIGATION_FAILED)
        if loader is not None and current.get("loaderId") != loader:
            raise ChromiumError(Failure.NAVIGATION_FAILED)
        final_url = current.get("url")
        fragment = current.get("urlFragment", "")
        if not isinstance(final_url, str) or not isinstance(fragment, str):
            raise ChromiumError(Failure.CDP_FAILED)
        final_url += fragment
        if (loader is None and final_url != url) or (
            url == BLACK_PAGE and final_url != BLACK_PAGE
        ):
            raise ChromiumError(Failure.NAVIGATION_FAILED)
        self.check(deadline, failure)
        return final_url

    @staticmethod
    def _loaded(method, params, frame, loader, url):
        if method in ("Page.javascriptDialogOpening", "Page.interstitialShown"):
            raise ChromiumError(Failure.NAVIGATION_FAILED)
        if method == "Page.lifecycleEvent":
            return (
                loader is not None
                and params.get("name") == "load"
                and params.get("frameId") == frame
                and params.get("loaderId") == loader
            )
        if method == "Page.navigatedWithinDocument":
            return (
                loader is None
                and params.get("frameId") == frame
                and params.get("url") == url
            )
        return False

    def close(self):
        try:
            self.connection.close()
        except (OSError, WebSocketException):
            raise ChromiumError(Failure.CLEANUP_FAILED) from None
