import os

import pytest

from postcardscene.filesystem_source import InvalidSource, PathPolicy, open_image_item


def test_descriptor_stays_on_original_after_name_replacement(tmp_path):
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"original")
    info = path.stat()
    with open_image_item(
        "local_directory",
        {"path": str(tmp_path), "recursive": True},
        PathPolicy([tmp_path]),
        "photo.jpg",
        info.st_size,
        info.st_mtime_ns,
    ) as stream:
        path.rename(tmp_path / "old.jpg")
        path.write_bytes(b"replaced")
        assert stream.read() == b"original"


@pytest.mark.parametrize(
    "case",
    [
        "final_symlink",
        "directory_symlink",
        "fifo",
        "directory",
        "nested",
        "stale_size",
        "stale_time",
    ],
)
def test_safe_open_refuses_unsafe_or_stale(tmp_path, case):
    root = tmp_path / "root"
    root.mkdir()
    child = root / "child"
    child.mkdir()
    path = child / "photo.jpg"
    path.write_bytes(b"original")
    info = path.stat()
    if case == "final_symlink":
        path.unlink()
        path.symlink_to(tmp_path / "secret")
    elif case == "directory_symlink":
        child.rename(root / "moved")
        child.symlink_to(root / "moved", target_is_directory=True)
    elif case == "fifo":
        path.unlink()
        os.mkfifo(path)
    elif case == "directory":
        path.unlink()
        path.mkdir()
    with pytest.raises(InvalidSource):
        with open_image_item(
            "local_directory",
            {"path": str(root), "recursive": case != "nested"},
            PathPolicy([root]),
            "child/photo.jpg",
            info.st_size + (case == "stale_size"),
            info.st_mtime_ns + (case == "stale_time"),
        ):
            pytest.fail("unsafe open succeeded")


@pytest.mark.parametrize("component", ["child", "photo.jpg"])
def test_symlink_replacement_at_actual_open(tmp_path, monkeypatch, component):
    child = tmp_path / "child"
    child.mkdir()
    path = child / "photo.jpg"
    path.write_bytes(b"original")
    info = path.stat()
    original_open = os.open

    def replace_at_open(name, flags, **kwargs):
        if name == component:
            target = tmp_path / "child" if component == "child" else path
            moved = target.with_name(target.name + "-moved")
            target.rename(moved)
            target.symlink_to(moved)
        return original_open(name, flags, **kwargs)

    monkeypatch.setattr(os, "open", replace_at_open)
    with pytest.raises(InvalidSource):
        with open_image_item(
            "local_directory",
            {"path": str(tmp_path), "recursive": True},
            PathPolicy([tmp_path]),
            "child/photo.jpg",
            info.st_size,
            info.st_mtime_ns,
        ):
            pytest.fail("replacement followed")
