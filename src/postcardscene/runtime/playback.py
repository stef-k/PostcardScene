"""One bounded mailbox and dedicated playback owner, wired by #93 later."""

from collections import deque
from concurrent.futures import Future
from functools import partial
from threading import Condition, Event, Thread
from time import monotonic

from postcardscene.display_planner import DisplayPlanner
from postcardscene.playback_configuration import resolve_active_sequence
from postcardscene.runtime.playback_state import PlaybackState, valid_command
from postcardscene.runtime.presentation import (
    Outcome,
    PlaybackStatus,
    PresentationError,
)

MAILBOX_LIMIT = 32
JOIN_SECONDS = 5.0


class PlaybackWorker:
    """Construct/start once; the host calls join after its stop_event wakes.

    submit never executes presenter work. Its Future resolves to a fixed Outcome
    on execution (or immediately for invalid/full/closed mailboxes). No executor
    pool is involved. All planner/presenter calls, including final stop, run on
    this one thread. The host must join on process cancellation to wake a dwell
    wait immediately. request_shutdown performs both cancellation and wakeup.
    """

    def __init__(
        self,
        database,
        presenter,
        stop_event,
        *,
        planner=None,
        configuration=None,
        clock=monotonic,
        wait=None,
    ):
        self.stop_event = stop_event
        self._condition = Condition()
        self._commands = deque()
        self._operation_cancel = Event()
        self._status = PlaybackStatus()
        self._failure = None
        self._closed = False
        self._wait = wait or (lambda condition, timeout: condition.wait(timeout))
        self._machine = PlaybackState(
            planner if planner is not None else DisplayPlanner(database),
            configuration or partial(resolve_active_sequence, database),
            presenter,
            clock,
            self._publish,
        )
        # An uncooperative backend must not prevent fatal process exit/reaping.
        self.thread = Thread(target=self._run, name="playback", daemon=True)

    @property
    def status(self):
        with self._condition:
            return self._status

    @property
    def failure(self):
        with self._condition:
            return self._failure

    def _publish(self, status):
        with self._condition:
            if self._failure is None:
                self._status = status

    def start(self):
        self.thread.start()

    def submit(self, name, value=None):
        result = Future()
        with self._condition:
            if (
                self._closed
                or self.stop_event.is_set()
                or not valid_command(name, value)
            ):
                result.set_result(Outcome.REJECTED)
            elif len(self._commands) >= MAILBOX_LIMIT:
                result.set_result(Outcome.BUSY)
            else:
                self._commands.append((name, value, result))
                if name in {
                    "next",
                    "previous",
                    "pause",
                    "resume",
                    "toggle_pause",
                    "set_output_suppressed",
                }:
                    self._operation_cancel.set()
                self._condition.notify_all()
        return result

    def set_output_suppressed(self, suppressed):
        return self.submit("set_output_suppressed", suppressed)

    def request_shutdown(self):
        self.stop_event.set()
        with self._condition:
            self._operation_cancel.set()
            self._condition.notify_all()

    def join(self):
        """Cancel and join within five seconds; never claim uncertain cleanup."""
        self.request_shutdown()
        self.thread.join(JOIN_SECONDS)
        if self.thread.is_alive():
            self._fatal("shutdown_timeout")
        if self.failure is not None:
            raise RuntimeError("Playback worker failed: " + self.failure)

    def _fatal(self, reason):
        with self._condition:
            if self._failure is None:
                self._failure = reason
            self._status = PlaybackStatus(state="error", reason=self._failure)
            self._closed = True
            self.stop_event.set()
            self._operation_cancel.set()
            self._condition.notify_all()

    def _run(self):
        try:
            self._loop()
        except PresentationError as error:
            self._fatal(
                "cleanup_failed"
                if error.outcome == Outcome.CLEANUP_FAILED
                else "worker_failed"
            )
        except BaseException:
            self._fatal("worker_failed")
        finally:
            self._machine.report("stopping", "stopping")
            try:
                self._machine.presenter.stop()
            except BaseException:
                self._fatal("cleanup_failed")
            with self._condition:
                self._closed = True
                pending = tuple(self._commands)
                self._commands.clear()
            for _, _, result in pending:
                if result.set_running_or_notify_cancel():
                    result.set_result(Outcome.REJECTED)
            self._machine.content = None
            self._machine.current = None
            self._machine.remaining = None
            self._machine.report("stopped", "stopped")

    def _loop(self):
        while not self.stop_event.is_set():
            with self._condition:
                command = self._commands.popleft() if self._commands else None
                # Replace only under the mailbox lock, so intent arriving during
                # an operation cannot be erased by an event.clear race.
                token = Event()
                self._operation_cancel = token

            def cancelled():
                return self.stop_event.is_set() or token.is_set()

            if command is not None:
                name, value, result = command
                if result.set_running_or_notify_cancel():
                    try:
                        outcome = self._machine.execute(
                            lambda cancel: self._machine.command(name, value, cancel),
                            cancelled,
                        )
                    except BaseException:
                        result.set_result(Outcome.CLEANUP_FAILED)
                        raise
                    result.set_result(outcome)
                continue
            timeout = self._machine.execute(self._machine.tick, cancelled)
            if isinstance(timeout, Outcome):
                timeout = 0.0
            with self._condition:
                if not self._commands and not self.stop_event.is_set():
                    self._wait(self._condition, timeout)
