"""Local candidate traversal through the filesystem Source contract."""

import os

import pytest

from postcardscene.filesystem_source import (
    EnumerationFailed,
    InvalidSource,
    MediaEntry,
    MediaType,
    PathPolicy,
    ScanCancelled,
    SourceUnavailable,
    enumerate_local_directory,
)


@pytest.fixture
def source(tmp_path):
    return {"path": str(tmp_path), "recursive": True}, PathPolicy([tmp_path])


@pytest.mark.parametrize("recursive", [False, True])
def test_sorted_candidates_and_freshness(source, tmp_path, recursive):
    config, policy = source
    config["recursive"] = recursive
    # Creation order differs from exact-name sorted depth-first traversal.
    names = [
        "z.Mp4",
        "é.JPG",
        "e\u0301.jpg",
        "B/inside.MOV",
        "A.PNG",
        ".hidden/x.HeIc",
        ".photo.AVIF",
        "B/deeper/photo.tiff",
        "B/a.WEBP",
        "unknown.txt",
        "camera.CR2",
        "camera.NEF",
        "camera.dng",
    ]
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not decoded")
        os.utime(path, ns=(1234567890123456789, 1234567890123456789))
    (tmp_path / "linked.JPG").symlink_to(tmp_path / "A.PNG")
    (tmp_path / "linked-directory").symlink_to(tmp_path / "B")
    os.mkfifo(tmp_path / "pipe.jpg")

    entries = list(enumerate_local_directory(config, policy))

    expected = [".photo.AVIF", "A.PNG", "e\u0301.jpg", "z.Mp4", "é.JPG"]
    if recursive:
        expected = [
            ".hidden/x.HeIc",
            ".photo.AVIF",
            "A.PNG",
            "B/a.WEBP",
            "B/deeper/photo.tiff",
            "B/inside.MOV",
            "e\u0301.jpg",
            "z.Mp4",
            "é.JPG",
        ]
    assert [entry.relative_path for entry in entries] == expected
    for entry in entries:
        info = (tmp_path / entry.relative_path).lstat()
        kind = (
            MediaType.VIDEO
            if entry.relative_path in ("z.Mp4", "B/inside.MOV")
            else MediaType.IMAGE
        )
        assert entry == MediaEntry(
            entry.relative_path, kind, info.st_size, info.st_mtime_ns
        )


def test_cancel_before_traversal(source):
    with pytest.raises(ScanCancelled):
        next(enumerate_local_directory(*source, cancelled=lambda: True))


def test_cancel_after_partial_yield(source, tmp_path):
    (tmp_path / "a.jpg").touch()
    (tmp_path / "b.jpg").touch()
    stopped = False
    entries = enumerate_local_directory(*source, cancelled=lambda: stopped)
    assert next(entries).relative_path == "a.jpg"
    stopped = True
    with pytest.raises(ScanCancelled):
        next(entries)


@pytest.mark.parametrize("subtree", [False, True])
def test_disappearance_keeps_siblings_but_is_incomplete(source, tmp_path, subtree):
    (tmp_path / "a.jpg").touch()
    disappearing = tmp_path / "b.jpg"
    disappearing.mkdir() if subtree else disappearing.touch()
    (tmp_path / "c.jpg").touch()
    entries = enumerate_local_directory(*source)
    assert next(entries).relative_path == "a.jpg"
    disappearing.rmdir() if subtree else disappearing.unlink()
    assert next(entries).relative_path == "c.jpg"
    with pytest.raises(EnumerationFailed):
        next(entries)


def test_failing_subtree_keeps_siblings(source, tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "a/private.jpg").touch()
    (tmp_path / "b.jpg").touch()
    original = os.open

    def denied(path, flags, *args, **kwargs):
        if path == "a":
            raise PermissionError("private host details")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    entries = enumerate_local_directory(*source)
    assert next(entries).relative_path == "b.jpg"
    with pytest.raises(EnumerationFailed) as error:
        next(entries)
    assert "private" not in str(error.value)


def test_replacement_symlink_is_not_traversed(source, tmp_path, monkeypatch):
    child = tmp_path / "a"
    child.mkdir()
    (tmp_path / "b.jpg").touch()
    original = os.open

    def replace(path, flags, *args, **kwargs):
        if path == "a":
            child.rmdir()
            child.symlink_to(tmp_path.parent)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace)
    entries = enumerate_local_directory(*source)
    assert next(entries).relative_path == "b.jpg"
    with pytest.raises(EnumerationFailed):
        next(entries)


@pytest.mark.parametrize("root_kind", ["missing", "file"])
def test_unavailable_root(source, tmp_path, root_kind):
    config, policy = source
    config["path"] += "/root"
    if root_kind == "file":
        (tmp_path / "root").touch()
    with pytest.raises(SourceUnavailable):
        next(enumerate_local_directory(config, policy))


@pytest.mark.parametrize("invalid", ["outside", "recursive", "escape"])
def test_invalid_configuration(source, tmp_path, invalid):
    config, policy = source
    if invalid == "outside":
        config["path"] = str(tmp_path.parent)
    elif invalid == "recursive":
        config["recursive"] = 1
    else:
        (tmp_path / "escape").symlink_to(tmp_path.parent)
        config["path"] += "/escape"
    with pytest.raises(InvalidSource):
        next(enumerate_local_directory(config, policy))
