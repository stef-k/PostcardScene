"""Cancellation-aware serialization of Wayland output mutation only."""

from threading import Lock


class DisplayMutationGuard:
    def __init__(self):
        self._lock = Lock()

    def acquire(self, cancelled):
        while not cancelled():
            if self._lock.acquire(timeout=0.05):
                if cancelled():
                    self._lock.release()
                    return False
                return True
        return False

    def release(self):
        self._lock.release()
