"""Minimal durable update phases; scratch is never recovery or phase authority."""

import json
import os
import re
import stat
from dataclasses import asdict, dataclass

from postcardscene_install_host import ROOT, metadata, read_regular, trusted_parent
from postcardscene_install_inputs import InstallError as InputError
from postcardscene_install_inputs import unique_object
from postcardscene_install_services import InstallError

STATE = ROOT / "update-state.json"
SCRATCH = ROOT / "update-state.json.postcardscene-update.new"
LIMIT = 4096


@dataclass(frozen=True)
class UpdateState:
    phase: str
    from_version: str
    from_wheel_sha256: str
    from_schema: str
    to_version: str
    to_wheel_sha256: str
    to_manifest_sha256: str
    to_schema: str


def parse(data):
    try:
        if len(data) > LIMIT:
            raise ValueError
        values = json.loads(data, object_pairs_hook=unique_object)
        if not isinstance(values, dict) or set(values) != set(
            UpdateState.__dataclass_fields__
        ):
            raise ValueError
        for key, value in values.items():
            pattern = (
                r"prepared|migrating|committed"
                if key == "phase"
                else r"[0-9][a-z0-9.]{0,127}"
                if key.endswith("version")
                else r"[0-9a-f]{64}"
                if key.endswith("sha256")
                else r"[a-zA-Z0-9_]{1,128}"
            )
            if not isinstance(value, str) or not re.fullmatch(pattern, value):
                raise ValueError
        if values["from_version"] == values["to_version"]:
            raise ValueError
        return UpdateState(**values)
    except (ValueError, TypeError, InputError) as error:
        raise InstallError("invalid_update_state") from error


def parent():
    trusted_parent(ROOT)
    metadata(ROOT, mode=0o755, kind=stat.S_ISDIR)


def sync_parent():
    fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read():
    parent()
    if not os.path.lexists(STATE):
        return None
    metadata(STATE, mode=0o600)
    return parse(read_regular(STATE, LIMIT))


def clean_scratch():
    parent()
    if os.path.lexists(SCRATCH):
        metadata(SCRATCH, mode=0o600)
        SCRATCH.unlink()
        sync_parent()


def write(state):
    # Validate complete content before touching either exact path. Existing final
    # authority must also remain recognizable; scratch contents are never read.
    data = json.dumps(asdict(state), sort_keys=True, separators=(",", ":")).encode()
    parse(data)
    read()
    clean_scratch()
    fd = os.open(SCRATCH, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchown(stream.fileno(), 0, 0)
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    metadata(SCRATCH, mode=0o600)
    os.replace(SCRATCH, STATE)
    sync_parent()


def remove():
    if read() is not None:
        STATE.unlink()
        sync_parent()
