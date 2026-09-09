"""Linux one-check children bound filesystem, NSS, SQLite and tool stalls."""

import multiprocessing
import os
import signal
import subprocess

from . import Check

CHECK_TIMEOUT = 5.0
OUTPUT_LIMIT = 16384


def _child(connection, function, identifier):
    os.setsid()
    # No library/config/tool output escapes the closed result pipe.
    with open(os.devnull, "wb") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    try:
        result = function(identifier)
    except Exception:
        result = Check(identifier, "unavailable", "query_failed")
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
                return Check(identifier, "unavailable", "query_failed")
        return Check(identifier, "unavailable", "deadline")
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
    allowed = {
        "LoadState": {"loaded", "not-found", "error", "bad-setting", "masked"},
        "UnitFileState": {
            "enabled",
            "disabled",
            "static",
            "masked",
            "",
            "enabled-runtime",
            "linked",
            "indirect",
        },
        "ActiveState": {
            "active",
            "inactive",
            "failed",
            "activating",
            "deactivating",
            "reloading",
            "maintenance",
            "refreshing",
        },
    }
    if fields.keys() != allowed.keys() or any(
        fields[k] not in values for k, values in allowed.items()
    ):
        raise ValueError("Unrecognized service state.")
    return fields
