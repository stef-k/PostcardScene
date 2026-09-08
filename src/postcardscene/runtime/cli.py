"""Packaged process boundary for the runtime/player host."""

import signal
import sys

from postcardscene.configuration import load_runtime_config
from postcardscene.filesystem_source import PathPolicy
from postcardscene.persistence import Database
from postcardscene.runtime import RuntimeHost


def main() -> int:
    previous_handlers = {}
    database = None
    try:
        config = load_runtime_config()
        policy = PathPolicy(config["MEDIA_ALLOWED_ROOTS"])
        database = Database(config["DATABASE_PATH"])
        database.check()
        host = RuntimeHost(database, policy, config)

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
    finally:
        if database is not None:
            database.engine.dispose()
    return 0
