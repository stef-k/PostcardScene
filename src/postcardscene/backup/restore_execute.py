"""Root-only restore transaction: local candidate, rollback, commit, activation."""

import os
import signal
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event, select

from postcardscene.accounts import Administrator
from postcardscene.catalog import invalidate_catalog
from postcardscene.persistence import Database

from . import capture, restore, restore_files, restore_host
from .files import BackupError
from .operation import restore_lock


@contextmanager
def interruptions():
    def interrupted(signum, frame):
        raise KeyboardInterrupt

    previous = {
        sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@contextmanager
def recovery_signals():
    # A second handled signal must not interrupt byte-exact rollback/retirement.
    previous = {
        sig: signal.signal(sig, signal.SIG_IGN)
        for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def validate_data(identities, immutable, *, invalidate):
    entries = restore_files.targets(identities)
    for entry in entries:
        restore_files.current(*entry)
    capture.validate_config(capture.CONFIG.read_bytes())
    if capture.KEY.stat().st_size != 32:
        raise BackupError("restore_key_invalid")
    if restore_host.immutable_authority() != immutable:
        raise BackupError("restore_host_invalid")
    database = Database(capture.DATABASE)
    deadline = time.monotonic() + 120
    event.listen(
        database.engine,
        "connect",
        lambda connection, record: connection.set_progress_handler(
            lambda: int(time.monotonic() >= deadline), 10000
        ),
    )
    mask = os.umask(0o007)
    try:
        database.check()
        with database.transaction() as session:
            if session.scalar(select(Administrator.id).limit(1)) is None:
                raise BackupError("restore_administrator_missing")
            if invalidate:
                invalidate_catalog(session)
        database.check()
    finally:
        database.engine.dispose()
        os.umask(mask)


class Transaction:
    def __init__(self, candidate, identities, immutable, rollback_root):
        self.candidate = candidate
        self.identities = identities
        self.immutable = immutable
        self.rollback_root = rollback_root
        self.captured = ()
        self.changed = False
        self.committed = False
        self.preserve = False

    def replace(self):
        restore_host.stop(self.identities)
        self.captured = restore_files.capture_current(
            self.rollback_root, self.identities
        )
        restore.revalidate_restore(self.candidate)
        for entry, identity in self.captured:
            if restore_files.current(*entry) != identity:
                raise BackupError("restore_current_changed")
        with restore.staging_directory(
            self.candidate.staging_root, self.candidate.root_identity
        ) as (source, _):
            # Mark conservatively before the first unlink/replace can take effect.
            self.changed = True
            restore_files.remove_sidecars(self.identities, self.captured)
            for entry in restore_files.targets(self.identities):
                member = next(
                    m for m in self.candidate.members if m.name == entry[0].name
                )
                restore_files.replace(
                    source, member.name, entry, (member.size, member.sha256)
                )
        validate_data(self.identities, self.immutable, invalidate=True)
        self.committed = True

    def fail(self):
        with recovery_signals():
            stopped = True
            try:
                restore_host.stop(self.identities)
            except (Exception, KeyboardInterrupt):
                stopped = False
            if self.committed:
                return "restore_activation_failed"
            if not self.changed:
                return (
                    "restore_failed_stopped"
                    if stopped
                    else "restore_manual_recovery_required"
                )
            try:
                if not stopped:
                    raise BackupError("restore_stop_failed")
                restore_files.rollback(
                    self.rollback_root, self.identities, self.captured
                )
            except (Exception, KeyboardInterrupt):
                self.preserve = True
                return "restore_manual_recovery_required"
            return "restore_failed_rolled_back"


def execute(candidate):
    restore.require_root()
    identities, immutable = restore_host.validate()
    # Also prove bounded raw readability before touching systemd. No SQLite here.
    for entry in restore_files.file_set(identities):
        restore_files.current(*entry)
    transaction = None
    scratch = None
    identity = None
    failure_handled = False
    try:
        with restore_lock():
            scratch = Path(
                tempfile.mkdtemp(prefix="postcardscene-rollback-", dir="/tmp")
            )
            with restore.staging_directory(scratch) as (_, identity):
                pass
            transaction = Transaction(candidate, identities, immutable, scratch)
            try:
                transaction.replace()
                restore_host.activate_services()
            except (Exception, KeyboardInterrupt):
                failure_handled = True
                raise BackupError(transaction.fail()) from None
        # Persistent timer wakes can now acquire the same backup inode.
        restore_host.activate_timer()
    except (Exception, KeyboardInterrupt):
        if transaction is not None and not failure_handled:
            raise BackupError(transaction.fail()) from None
        raise
    finally:
        if (
            scratch is not None
            and identity is not None
            and not (transaction and transaction.preserve)
        ):
            try:
                restore.cleanup_staging(scratch, identity)
            except (Exception, KeyboardInterrupt):
                raise BackupError("restore_cleanup_uncertain") from None


def restore_archive(path):
    restore.require_root()
    candidate = None
    with interruptions():
        root = Path(tempfile.mkdtemp(prefix="postcardscene-restore-", dir="/tmp"))
        try:
            candidate = restore.prepare_restore(path, root)
            execute(candidate)
            return {"result": "restore_complete"}
        finally:
            if candidate is not None:
                try:
                    restore.cleanup_staging(root, candidate.root_identity)
                except (Exception, KeyboardInterrupt):
                    raise BackupError("restore_cleanup_uncertain") from None
