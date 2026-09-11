"""Exact committed host assets and atomic forward-only replacement."""

import os
import stat

import postcardscene_install_host as host
from postcardscene_install_services import InstallError

SUFFIX = ".postcardscene-update.new"


def scratch(path):
    return path.with_name(path.name + SUFFIX)


def sync(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def inspect_file(path, target, old=None):
    host.trusted_parent(path)
    host.metadata(path)
    content = host.read_regular(path)
    if content != target and (old is None or content != old):
        raise InstallError("update_asset_unknown")
    pending = scratch(path)
    if os.path.lexists(pending):
        host.validate_asset(pending, target)
    return content == target


def replace_file(path, target, old=None):
    if inspect_file(path, target, old):
        if os.path.lexists(scratch(path)):
            scratch(path).unlink()
            sync(path.parent)
        return
    pending = scratch(path)
    if not os.path.lexists(pending):
        fd = os.open(
            pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644
        )
        with os.fdopen(fd, "wb") as stream:
            os.fchown(stream.fileno(), 0, 0)
            os.fchmod(stream.fileno(), 0o644)
            stream.write(target)
            stream.flush()
            os.fsync(stream.fileno())
    host.validate_asset(pending, target)
    os.replace(pending, path)
    sync(path.parent)


def inspect_link(state):
    active = host.ROOT / "venv"
    target = host.RELEASES / state.to_version / "venv"
    host.metadata(active, mode=0o777, kind=stat.S_ISLNK)
    if active.readlink() not in (host.RELEASES / state.from_version / "venv", target):
        raise InstallError("update_active_unknown")
    pending = scratch(active)
    if os.path.lexists(pending):
        host.metadata(pending, mode=0o777, kind=stat.S_ISLNK)
        if pending.readlink() != target:
            raise InstallError("update_symlink_scratch_unknown")
    return active.readlink() == target


def replace_link(state):
    converged = inspect_link(state)
    active = host.ROOT / "venv"
    pending = scratch(active)
    if converged:
        if os.path.lexists(pending):
            pending.unlink()
            sync(host.ROOT)
        return
    if not os.path.lexists(pending):
        pending.symlink_to(host.RELEASES / state.to_version / "venv")
    sync(host.ROOT)
    os.replace(pending, active)
    sync(host.ROOT)
