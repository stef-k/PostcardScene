"""Linux one-check children bound filesystem, NSS, SQLite and tool stalls."""

import multiprocessing
import os
import signal
import subprocess
import sys

from . import SERVICE_VALUES, Check

CHECK_TIMEOUT = 5.0
OUTPUT_LIMIT = 16384


def unavailable(identifier, reason):
    state = (
        "fatal"
        if identifier in {"release", "config_authority", "identities"}
        else "unavailable"
    )
    return Check(identifier, state, reason)


def _child(connection, function, identifier):
    os.setsid()
    # No library/config/tool output escapes the closed result pipe.
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        sys.stdout = sys.stderr = sink
        try:
            result = function(identifier)
        except Exception:
            result = unavailable(identifier, "query_failed")
        connection.send(result)
        connection.close()


def inspect_bounded(function, identifier):
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_child, args=(child, function, identifier))
    process.start()
    child.close()
    try:
        if parent.poll(CHECK_TIMEOUT):
            try:
                return parent.recv()
            except EOFError:
                return unavailable(identifier, "query_failed")
        return unavailable(identifier, "deadline")
    finally:
        # Kill the whole check group, including descendants from fixed probes.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            if process.is_alive():
                process.kill()
        process.join(timeout=0.25)
        parent.close()


def systemd_state(unit):
    # The external tool is also bounded by its owning check child. Read one byte
    # beyond the cap and reject floods without retaining arbitrary tool output.
    command = (
        "/usr/bin/systemctl",
        "show",
        unit,
        "--no-pager",
        "--property=LoadState,UnitFileState,ActiveState",
    )
    with subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    ) as process:
        raw = process.stdout.read(OUTPUT_LIMIT + 1)
        if len(raw) > OUTPUT_LIMIT:
            process.kill()
            raise ValueError("Oversized query.")
        if process.wait(timeout=1):
            raise ValueError("Failed query.")
    fields = dict(line.split("=", 1) for line in raw.decode("ascii").splitlines())
    if fields.keys() != SERVICE_VALUES.keys() or any(
        fields[k] not in values for k, values in SERVICE_VALUES.items()
    ):
        raise ValueError("Unrecognized service state.")
    return fields
