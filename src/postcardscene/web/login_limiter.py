"""Bounded process-local login admission for concurrent Waitress requests."""

from collections import OrderedDict
from dataclasses import dataclass, field
from math import ceil
from threading import Lock
from time import monotonic


@dataclass
class FailureWindow:
    failures: list[float] = field(default_factory=list)
    pending: int = 0


class LoginLimiter:
    """Reserve capacity before password verification; never hold a lock across I/O."""

    def __init__(self):
        self._entries = OrderedDict()
        self._lock = Lock()

    def _prune(self, now):
        for ip, entry in list(self._entries.items()):
            entry.failures[:] = [t for t in entry.failures if t > now - 300]
            if not entry.failures and not entry.pending:
                del self._entries[ip]

    def _retry(self, entry, now):
        if entry and len(entry.failures) + entry.pending >= 5:
            return (
                max(1, min(300, ceil(entry.failures[0] + 300 - now)))
                if entry.failures
                else 1
            )
        return 0

    def retry_after(self, ip):
        with self._lock:
            now = monotonic()
            self._prune(now)
            return self._retry(self._entries.get(ip), now)

    def begin(self, ip):
        with self._lock:
            now = monotonic()
            self._prune(now)
            entry = self._entries.get(ip)
            retry = self._retry(entry, now)
            if retry:
                return None, retry
            if entry is None:
                if len(self._entries) == 256:
                    victim = next(
                        (
                            key
                            for key, value in self._entries.items()
                            if not value.pending
                        ),
                        None,
                    )
                    if victim is None:
                        return None, 1
                    del self._entries[victim]
                entry = self._entries[ip] = FailureWindow()
            self._entries.move_to_end(ip)
            entry.pending += 1
            return entry, 0

    def finish(self, ip, entry, *, success=False, failed=False):
        with self._lock:
            entry.pending -= 1
            if success:
                entry.failures.clear()
            elif failed:
                entry.failures.append(monotonic())
            self._entries.move_to_end(ip)
            if not entry.pending and not entry.failures:
                del self._entries[ip]
