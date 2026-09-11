"""Fixed installer recovery calls and the canonical backup mutation lock."""

import fcntl
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import postcardscene_install_host as host
from postcardscene_install_inputs import unique_object
from postcardscene_install_services import InstallError


@contextmanager
def mutation_lock():
    """Bootstrap/contend exactly as backup.operation.restore_lock, without imports
    from a caller environment or execution of old application code as root.
    """
    if os.geteuid() != 0:
        raise InstallError("root_required")
    _, web, shared, private = host.preserved_identities()
    root = host.KEY.parent
    host.trusted_parent(root)
    host.metadata(root, web, private, 0o700, stat.S_ISDIR)
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        bootstrap_lock(parent, web, shared)
        fd = os.open(
            "backup.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
                != (web, shared, 0o600)
            ):
                raise InstallError("operation_lock_invalid")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise InstallError("operation_busy") from None
            current = os.stat("backup.lock", dir_fd=parent, follow_symlinks=False)
            if not os.path.samestat(current, info):
                raise InstallError("operation_lock_invalid")
            yield fd
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def bootstrap_lock(parent, web, shared):
    try:
        fd = os.open(
            "backup.lock",
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
    except FileExistsError:
        return
    try:
        os.fchown(fd, web, shared)
        os.fchmod(fd, 0o600)
        os.fsync(fd)
        os.fsync(parent)
    finally:
        os.close(fd)


# Fixed code is authenticated installer input. Only the old environment imports
# the old backup API. Neither archived bytes nor a caller module can be executed.
POLICY = """
import json
from postcardscene.backup_policy import get_backup_policy
from postcardscene.persistence import Database
database = Database()
try:
    policy, _ = get_backup_policy(database)
    print(json.dumps(policy.destination_path))
finally:
    database.engine.dispose()
"""
CREATE = """
import json, sys
from dataclasses import asdict
from postcardscene.backup import _create_locked
print(json.dumps(asdict(_create_locked(sys.argv[1], lock_fd=int(sys.argv[2])))))
"""
VERIFY = """
import json, sys
from dataclasses import asdict
from postcardscene.backup import verify
print(json.dumps(asdict(verify(sys.argv[1]))))
"""


@dataclass(frozen=True)
class VerifiedBackup:
    """Wire copy of the old API's complete immutable result, never persisted."""

    archive_filename: str
    archive_sha256: str
    created_at_utc: str
    application_version: str
    sqlite_application_id: int
    schema_revision: str
    catalog_classification: str


def result(data):
    try:
        values = json.loads(data, object_pairs_hook=unique_object)
        verified = VerifiedBackup(**values)
        if (
            any(
                not isinstance(value, str)
                for key, value in values.items()
                if key != "sqlite_application_id"
            )
            or type(verified.sqlite_application_id) is not int
        ):
            raise ValueError
        if (
            not verified.archive_filename
            or Path(verified.archive_filename).name != verified.archive_filename
            or verified.archive_filename in {".", ".."}
        ):
            raise ValueError
        return verified
    except (ValueError, TypeError) as error:
        raise InstallError("recovery_result_invalid") from error


def absolute_path(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 4096
        or "\x00" in value
        or not Path(value).is_absolute()
        or ".." in Path(value).parts
    ):
        raise InstallError("recovery_path_invalid")
    return Path(value)


class Recovery:
    def __init__(self, current, lock_fd, run):
        self.current = current
        self.lock_fd = lock_fd
        self.run = run
        self.python = str(host.RELEASES / current["version"] / "venv/bin/python")
        self.selected = None
        self.verified = None

    def call(self, code, *arguments):
        return self.run(
            (self.python, "-I", "-B", "-c", code, *arguments),
            user="postcardscene-web",
            capture=True,
            pass_fds=(self.lock_fd,),
            timeout=330,
        )

    def select(self, destination=None, archive=None):
        if destination is not None and archive is not None:
            raise InstallError("recovery_selection_conflict")
        created = None
        if archive is None:
            if destination is None:
                destination = json.loads(self.call(POLICY))
            directory = absolute_path(destination)
            created = result(self.call(CREATE, str(directory), str(self.lock_fd)))
            archive = str(directory / created.archive_filename)
        self.selected = absolute_path(archive)
        self.verified = created
        return self.reverify()

    def reverify(self):
        verified = result(self.call(VERIFY, str(self.selected)))
        if (
            verified.application_version != self.current["version"]
            or verified.schema_revision != self.current["schema"]
            or verified.sqlite_application_id != self.current["application_id"]
            or verified.archive_filename != self.selected.name
            or (self.verified is not None and verified != self.verified)
        ):
            raise InstallError("recovery_identity_mismatch")
        self.verified = verified
        return verified
