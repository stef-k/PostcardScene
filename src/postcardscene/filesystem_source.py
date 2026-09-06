"""Filesystem semantics, independent of persistence and Flask.

Enumerators (#23/#24) yield MediaEntry incrementally. Only normal exhaustion is
complete: every FilesystemSourceError means incomplete traversal and forbids
absence-based deletion. Observe the runtime stop predicate between operations
and yields, raising ScanCancelled rather than returning. Never hold a database
transaction during traversal. Kernel filesystem calls can hang; #24 must bound
mounted-storage operations outside this synchronous path-checking seam.
"""

import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class FilesystemSourceError(Exception):
    """Filesystem operation did not complete authoritatively."""


class InvalidSource(FilesystemSourceError, ValueError):
    """Invalid configuration, identity, or path authority."""


class SourceUnavailable(FilesystemSourceError):
    """Storage is missing, unreadable, or not currently a directory/file."""


class ScanCancelled(FilesystemSourceError):
    """Cooperative stop requested; never translate to successful exhaustion."""


class EnumerationFailed(FilesystemSourceError):
    """Unexpected traversal failure; previously yielded entries are partial."""


class SourceStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class MediaType(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


def _absolute_path(value: object) -> Path:
    if not isinstance(value, str) or not value.startswith("/"):
        raise InvalidSource("Path must be an absolute Linux path string.")
    # Literal configuration only: never expand shell, glob, or URI input.
    if any(c in value for c in "\0~$`*?[]{}();|&<>\\\n\r\"'") or "://" in value:
        raise InvalidSource("Path must not contain expansion or command syntax.")
    if ".." in value.split("/"):
        raise InvalidSource("Parent path components are forbidden.")
    return Path("/" + "/".join(p for p in value.split("/") if p not in ("", ".")))


@dataclass(frozen=True)
class PathPolicy:
    """Trusted host authority, never populated from ordinary Source JSON/UI."""

    allowed_roots: tuple[Path, ...]

    def __post_init__(self):
        if not isinstance(self.allowed_roots, (tuple, list)):
            raise InvalidSource("Allowed roots must be an explicit list or tuple.")
        roots = tuple(_absolute_path(str(p)) for p in self.allowed_roots)
        if Path("/") in roots:
            raise InvalidSource("The filesystem root cannot be allowed authority.")
        object.__setattr__(self, "allowed_roots", roots)

    def check(self, path: Path) -> None:
        """Check lexical and resolvable canonical authority; I/O may propagate."""
        roots = [root for root in self.allowed_roots if path.is_relative_to(root)]
        if not roots:
            raise InvalidSource("Source is outside allowed media roots.")
        # Non-strict resolution also detects escapes through existing ancestors
        # when the final path is missing. Strict availability is checked separately.
        canonical = path.resolve()
        for root in roots:
            authority = root.resolve()
            if authority != Path("/") and canonical.is_relative_to(authority):
                return
        raise InvalidSource("Source canonical path escapes allowed authority.")


def validate_source(kind: object, configuration: object, policy: PathPolicy) -> dict:
    """Return normalized whole JSON config; unavailable storage remains valid.

    Operational adapters and #25 must call this before use/persistence. Generic
    domain create/update remains structural only. Probing may block in the OS.
    """
    if not isinstance(kind, str) or kind not in (
        "local_directory",
        "mounted_directory",
    ):
        raise InvalidSource("Unsupported filesystem Source kind.")
    if type(configuration) is not dict or set(configuration) != {"path", "recursive"}:
        raise InvalidSource("Configuration requires exactly path and recursive.")
    if type(configuration["recursive"]) is not bool:
        raise InvalidSource("Recursive must be a boolean.")
    path = _absolute_path(configuration["path"])
    try:
        policy.check(path)
    except (OSError, RuntimeError):
        # RuntimeError is pathlib's symlink-loop result on Python 3.11/3.12.
        # No successful availability claim follows a failed canonical probe.
        pass
    return {"path": str(path), "recursive": configuration["recursive"]}


def _source_directory(kind: object, configuration: object, policy: PathPolicy) -> Path:
    normalized = validate_source(kind, configuration, policy)
    path = Path(normalized["path"])
    policy.check(path)
    canonical = path.resolve(strict=True)
    if not stat.S_ISDIR(canonical.stat().st_mode):
        raise SourceUnavailable("Source is not a directory.")
    if not os.access(canonical, os.R_OK | os.X_OK, effective_ids=True):
        raise SourceUnavailable("Source is unreadable.")
    return canonical


def source_status(
    kind: object, configuration: object, policy: PathPolicy
) -> SourceStatus:
    """Safe one-shot status, without raw error text or a syscall time guarantee."""
    try:
        _source_directory(kind, configuration, policy)
    except InvalidSource:
        return SourceStatus.INVALID
    except (SourceUnavailable, OSError, RuntimeError):
        return SourceStatus.UNAVAILABLE
    return SourceStatus.AVAILABLE


def validate_relative_path(value: object) -> str:
    """Validate canonical identity without rewriting Linux case or Unicode."""
    if not isinstance(value, str) or "\0" in value:
        raise InvalidSource("Item identity must be a relative path string.")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise InvalidSource(
            "Item identity must contain only normal relative components."
        )
    return value


def resolve_item_path(
    kind: object, configuration: object, policy: PathPolicy, relative_path: str
) -> Path:
    """Re-check current authority and every descendant; accept regular files only.

    This is a point-in-time path check, not an open file capability. Consumers
    must not cache its result as authority or assume immunity to concurrent
    rename/replacement between checking and opening a file.
    """
    relative_path = validate_relative_path(relative_path)
    try:
        current = _source_directory(kind, configuration, policy)
        parts = relative_path.split("/")
        if not configuration["recursive"] and len(parts) != 1:
            raise InvalidSource("Nonrecursive Sources allow only direct children.")
        for index, part in enumerate(parts):
            current = current / part
            mode = current.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise InvalidSource("Symlink descendants are forbidden.")
            expected = stat.S_ISREG if index == len(parts) - 1 else stat.S_ISDIR
            if not expected(mode):
                raise InvalidSource(
                    "Item must use normal directories and a regular file."
                )
        root = _source_directory(kind, configuration, policy)
        if not current.resolve(strict=True).is_relative_to(root):
            raise InvalidSource("Item escapes Source authority.")
        return current
    except (OSError, RuntimeError) as error:
        raise SourceUnavailable("Item storage is unavailable.") from error


@dataclass(frozen=True)
class MediaEntry:
    """Candidate occurrence; discovery does not prove playback support."""

    relative_path: str
    media_type: MediaType
    size_bytes: int
    mtime_ns: int

    def __post_init__(self):
        validate_relative_path(self.relative_path)
        try:
            object.__setattr__(self, "media_type", MediaType(self.media_type))
        except (ValueError, TypeError) as error:
            raise InvalidSource("Media type must be image or video.") from error
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise InvalidSource("Size must be a nonnegative integer.")
        if type(self.mtime_ns) is not int:
            raise InvalidSource("Modification freshness must be an integer.")
