"""Behavioral filesystem contract tests; no Flask, catalog, or media decoder."""

import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from postcardscene.filesystem_source import (
    EnumerationFailed,
    FilesystemSourceError,
    InvalidSource,
    MediaEntry,
    MediaType,
    PathPolicy,
    ScanCancelled,
    SourceStatus,
    SourceUnavailable,
    resolve_item_path,
    source_status,
    validate_relative_path,
    validate_source,
)


@pytest.fixture
def source(tmp_path):
    return {"path": str(tmp_path), "recursive": True}, PathPolicy([tmp_path])


@pytest.mark.parametrize("kind", ["local_directory", "mounted_directory"])
def test_normalized_source(kind, source):
    config, policy = source
    config["path"] += "//./Photos/"
    normalized = validate_source(kind, config, policy)
    assert normalized == {
        "path": str(policy.allowed_roots[0] / "Photos"),
        "recursive": True,
    }
    assert source_status(kind, normalized, policy) == SourceStatus.UNAVAILABLE
    Path(normalized["path"]).mkdir()
    assert source_status(kind, normalized, policy) == SourceStatus.AVAILABLE


@pytest.mark.parametrize(
    "path",
    [
        "",
        "relative",
        "~/media",
        "/tmp/~user",
        "/tmp/$HOME",
        "/tmp/$(id)",
        "/tmp/`id`",
        "/tmp/*.jpg",
        "/tmp/a;id",
        "file:///tmp",
        "/tmp/http://x",
        "/tmp/\0",
        "/tmp/../media",
        "/tmp/a/../../b",
        1,
        None,
    ],
)
def test_reject_path_syntax(path, source):
    config, policy = source
    config["path"] = path
    with pytest.raises(InvalidSource):
        validate_source("local_directory", config, policy)


@pytest.mark.parametrize(
    "config",
    [
        None,
        [],
        {},
        {"path": "/tmp"},
        {"path": "/tmp", "recursive": 1},
        {"path": "/tmp", "recursive": "true"},
        {"path": "/tmp", "recursive": True, "roots": ["/"]},
    ],
)
def test_strict_configuration(config, source):
    _, policy = source
    assert source_status("local_directory", config, policy) == SourceStatus.INVALID


def test_authority_and_kind(source, tmp_path):
    config, policy = source
    with pytest.raises(InvalidSource):
        validate_source("web_url", config, policy)
    config["path"] = str(tmp_path) + "-sibling"
    with pytest.raises(InvalidSource):
        validate_source("local_directory", config, policy)
    with pytest.raises(InvalidSource):
        PathPolicy(["//./"])
    assert (
        source_status("local_directory", config, PathPolicy([])) == SourceStatus.INVALID
    )


def test_canonical_source_escape(source, tmp_path):
    config, policy = source
    (tmp_path / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    config["path"] += "/escape"
    with pytest.raises(InvalidSource):
        validate_source("local_directory", config, policy)
    assert source_status("local_directory", config, policy) == SourceStatus.INVALID
    config["path"] += "/missing"
    assert source_status("local_directory", config, policy) == SourceStatus.INVALID


def test_source_alias_within_authority_and_non_directory(source, tmp_path):
    config, policy = source
    (tmp_path / "real").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "real", target_is_directory=True)
    config["path"] += "/alias"
    assert source_status("local_directory", config, policy) == SourceStatus.AVAILABLE
    (tmp_path / "file").touch()
    config["path"] = str(tmp_path / "file")
    assert source_status("local_directory", config, policy) == SourceStatus.UNAVAILABLE


@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError, OSError])
def test_availability_safe_io_failure(error, monkeypatch, source):
    config, policy = source

    def fail(*args, **kwargs):
        raise error("private host details")

    monkeypatch.setattr(Path, "stat", fail)
    assert validate_source("mounted_directory", config, policy) == config
    status = source_status("mounted_directory", config, policy)
    assert status == SourceStatus.UNAVAILABLE
    assert "private" not in str(status)


@pytest.mark.parametrize(
    "relative",
    ["", "/absolute", ".", "..", "a/../b", "a/./b", "a//b", "a/", "a\0", None],
)
def test_relative_identity_rejection(relative):
    with pytest.raises(InvalidSource):
        validate_relative_path(relative)


def test_safe_resolution_preserves_occurrences(source, tmp_path):
    config, policy = source
    relative = "Photos/Été e\u0301.JPG"
    target = tmp_path / relative
    target.parent.mkdir()
    target.write_bytes(b"photo")
    assert resolve_item_path("local_directory", config, policy, relative) == target
    assert validate_relative_path(relative) == relative
    config["recursive"] = False
    with pytest.raises(InvalidSource):
        resolve_item_path("local_directory", config, policy, relative)
    (tmp_path / "direct").touch()
    assert (
        resolve_item_path("local_directory", config, policy, "direct")
        == tmp_path / "direct"
    )


def test_symlinks_special_files_and_replaced_catalog_path(source, tmp_path):
    config, policy = source
    (tmp_path / "normal").touch()
    assert (
        resolve_item_path("local_directory", config, policy, "normal")
        == tmp_path / "normal"
    )
    (tmp_path / "normal").unlink()
    (tmp_path / "normal").symlink_to(tmp_path / "other")
    (tmp_path / "directory").symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    os.mkfifo(tmp_path / "fifo")
    (tmp_path / "folder").mkdir()
    for relative in ("normal", "directory/normal", "escape/anything", "fifo", "folder"):
        with pytest.raises(InvalidSource):
            resolve_item_path("local_directory", config, policy, relative)
    with pytest.raises(SourceUnavailable):
        resolve_item_path("local_directory", config, policy, "missing")


@pytest.mark.parametrize("media_type", ["image", "video"])
def test_entry(media_type, tmp_path):
    target = tmp_path / "Été.JPG"
    target.write_bytes(b"candidate")
    info = target.stat()
    entry = MediaEntry(target.name, media_type, info.st_size, info.st_mtime_ns)
    assert entry.media_type == MediaType(media_type)
    assert entry.size_bytes == 9
    assert entry.mtime_ns == info.st_mtime_ns
    assert entry.relative_path == target.name
    with pytest.raises(FrozenInstanceError):
        entry.size_bytes = 0


@pytest.mark.parametrize(
    "args",
    [
        ("a", "audio", 0, 0),
        ("a", "image", -1, 0),
        ("a", "image", True, 0),
        ("a", "image", 0, 1.5),
        ("a", "image", 0, True),
        ("../a", "image", 0, 0),
    ],
)
def test_invalid_entry(args):
    with pytest.raises(InvalidSource):
        MediaEntry(*args)


def test_incomplete_errors_are_not_successful_exhaustion():
    for error in (InvalidSource, SourceUnavailable, ScanCancelled, EnumerationFailed):
        assert issubclass(error, FilesystemSourceError)
        assert not issubclass(error, StopIteration)


def test_unreadable_directory(source, monkeypatch):
    config, policy = source
    monkeypatch.setattr(os, "access", lambda *args, **kwargs: False)
    assert source_status("local_directory", config, policy) == SourceStatus.UNAVAILABLE


def test_canonical_policy_cannot_authorize_entire_host(tmp_path):
    alias = tmp_path / "host"
    alias.symlink_to("/", target_is_directory=True)
    config = {"path": str(alias), "recursive": True}
    assert (
        source_status("local_directory", config, PathPolicy([alias]))
        == SourceStatus.INVALID
    )
