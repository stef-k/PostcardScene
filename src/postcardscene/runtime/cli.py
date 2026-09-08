"""Packaged process boundary for the runtime/player host."""

import logging
import signal

from postcardscene.configuration import load_runtime_config
from postcardscene.filesystem_source import PathPolicy
from postcardscene.persistence import Database
from postcardscene.runtime import RuntimeHost


def main() -> int:
    logger = logging.getLogger("postcardscene.runtime.lifecycle")
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    previous_level, previous_propagate = logger.level, logger.propagate
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        return _run(logger)
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


def _run(logger) -> int:
    logger.info("Runtime startup beginning.")
    try:
        _serve()
    except Exception:
        logger.error("Runtime failed and cannot continue.")
        return 1
    logger.info("Runtime shutdown complete.")
    return 0


def _serve() -> None:
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
    except BaseException as error:
        if database is not None:
            try:
                database.engine.dispose()
            except BaseException:
                error.add_note("Runtime database cleanup also failed.")
        raise
    else:
        database.engine.dispose()
