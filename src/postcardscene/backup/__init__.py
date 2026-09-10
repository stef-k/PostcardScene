"""Backup API. Destination filesystem work runs in a bounded disposable process."""

import json
import os
import selectors
import subprocess
import sys
import time

from .archive import VerifiedBackup
from .destination import BackupEntry
from .files import BackupError
from .operation import operation_lock

__all__ = ["BackupError", "VerifiedBackup", "create", "verify", "list_backups"]


def _receive(process, cancelled, timeout, idle_timeout):
    started = last_progress = time.monotonic()
    pending = b""
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            now = time.monotonic()
            if (
                cancelled()
                or now - started >= timeout
                or now - last_progress >= idle_timeout
            ):
                raise BackupError("operation_timeout_or_cancelled")
            if not selector.select(min(0.1, timeout, idle_timeout)):
                continue
            block = os.read(process.stdout.fileno(), 65536)
            if not block:
                raise BackupError("worker_failed")
            pending += block
            if len(pending) > 256 * 1024:
                raise BackupError("worker_protocol_failed")
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if line == b"progress":
                    last_progress = time.monotonic()
                    continue
                message = json.loads(line)
                if "result" not in message:
                    raise BackupError("backup_failed")
                return message["result"]


def _run(
    operation,
    path,
    *,
    arguments=(),
    cancelled=lambda: False,
    timeout=300.0,
    idle_timeout=30.0,
):
    if not 0 < timeout <= 300 or not 0 < idle_timeout <= 30:
        raise ValueError("Invalid backup operation bounds.")
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "postcardscene.backup.worker",
            operation,
            os.fspath(path),
            *arguments,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        cwd="/",
    )
    try:
        result = _receive(process, cancelled, timeout, idle_timeout)
        if process.wait(timeout=1) != 0:
            raise BackupError("worker_failed")
        return result
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                # A kernel-stalled process may remain kill-pending. Never join it
                # indefinitely or report a completed operation with uncertain cleanup.
                raise BackupError("worker_cleanup_uncertain") from None
        process.stdout.close()


def create(destination, **bounds):
    with operation_lock():
        return _create_locked(destination, **bounds)


def _create_locked(destination, **bounds):
    return VerifiedBackup(**_run("create", destination, **bounds))


def verify(archive, **bounds):
    return VerifiedBackup(**_run("verify", archive, **bounds))


def list_backups(destination, **bounds):
    return tuple(BackupEntry(**entry) for entry in _run("list", destination, **bounds))
