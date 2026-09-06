"""Packaged process boundary for the runtime/player host."""

import signal
import sys

from postcardscene.runtime import RuntimeHost


def main() -> int:
    previous_handlers = {}
    try:
        host = RuntimeHost()

        def request_shutdown(signum, frame):
            host.request_shutdown()

        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[signum] = signal.signal(signum, request_shutdown)
            host.run()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
    except Exception:
        # No raw exception text: operator diagnostics are a later logging boundary.
        print("Runtime failed and cannot continue.", file=sys.stderr)
        return 1
    return 0
