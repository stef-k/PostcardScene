"""Video configuration and catalog selection, without media files or a player."""

from dataclasses import FrozenInstanceError, replace

import pytest

from postcardscene import domain as d
from postcardscene import video_selection as videos
from postcardscene.catalog import MediaItem


@pytest.mark.parametrize(
    "configuration,expected",
    [
        ({}, {"audio_enabled": False, "volume": 50}),
        ({"audio_enabled": True}, {"audio_enabled": True, "volume": 50}),
        ({"volume": 100}, {"audio_enabled": False, "volume": 100}),
        ({"audio_enabled": True, "volume": 0}, {"audio_enabled": True, "volume": 0}),
        (
            {"audio_enabled": False, "volume": 100},
            {"audio_enabled": False, "volume": 100},
        ),
    ],
)
def test_configuration(configuration, expected):
    assert videos.validate_video_configuration("video", configuration) == expected


@pytest.mark.parametrize(
    "configuration",
    [
        None,
        [],
        {"audio_enabled": 1},
        {"audio_enabled": "false"},
        {"volume": True},
        {"volume": False},
        {"volume": 1.5},
        {"volume": "50"},
        {"volume": None},
        {"volume": -1},
        {"volume": 101},
        {"muted": True},
        {"duration": 10},
        {"audio_device": "default"},
    ],
)
def test_invalid_configuration(configuration):
    with pytest.raises(videos.VideoSelectionError):
        videos.validate_video_configuration("video", configuration)


@pytest.mark.parametrize("kind", ["image", "portrait_image_pair", "web_view", None, []])
def test_wrong_kind(kind):
    with pytest.raises(videos.VideoSelectionError):
        videos.validate_video_configuration(kind, {})


@pytest.fixture
def selection(catalog):
    db, source_id, _, _ = catalog
    with db.transaction() as session:
        widget_id = d.create_widget(
            session, name="Video", kind="video", configuration={}, source_id=source_id
        ).id
    return db, source_id, widget_id


def add_item(session, source_id, path="clip.mp4", **changes):
    fields = dict(
        source_id=source_id,
        relative_path=path,
        media_type="video",
        size_bytes=123,
        mtime_ns=-456,
        seen_generation=1,
    )
    fields.update(changes)
    item = MediaItem(**fields)
    session.add(item)
    session.flush()
    return item


@pytest.mark.parametrize(
    "problem",
    [
        "missing_widget",
        "disabled_widget",
        "wrong_kind",
        "no_source",
        "missing_source",
        "disabled_source",
        "web_source",
        "configuration",
    ],
)
def test_invalid_context(selection, problem):
    db, source_id, widget_id = selection
    with db.transaction() as session:
        widget = d.get_widget(session, widget_id)
        source = d.get_source(session, source_id)
        if problem == "missing_widget":
            widget_id += 100
        elif problem == "disabled_widget":
            widget.enabled = False
        elif problem == "wrong_kind":
            widget.kind = "image"
        elif problem == "no_source":
            widget.source_id = None
        elif problem == "missing_source":
            widget.source_id = source_id + 100
        elif problem == "disabled_source":
            source.enabled = False
        elif problem == "configuration":
            widget.configuration = {"muted": True}
        else:
            source.kind = "web_url"
        for query in (
            lambda: videos.resolve_video_context(session, widget_id),
            lambda: videos.get_selected_video(
                session, widget_id, source_id, "clip.mp4"
            ),
            lambda: videos.list_selected_videos(session, widget_id),
        ):
            with pytest.raises(videos.VideoSelectionError):
                query()
        session.rollback()


@pytest.mark.parametrize("kind", ["local_directory", "mounted_directory"])
def test_snapshots_and_pages(selection, kind):
    db, source_id, widget_id = selection
    with db.transaction() as session:
        d.get_source(session, source_id).kind = kind
        first = add_item(session, source_id)
        add_item(session, source_id, "image.jpg", media_type="image")
        last = add_item(session, source_id, "other.mkv", metadata_status="error")
        other = d.create_source(session, name="Other", kind=kind, configuration={}).id
        add_item(session, other)
        context = videos.resolve_video_context(session, widget_id)
        assert (context.source_id, context.audio_enabled, context.volume) == (
            source_id,
            False,
            50,
        )
        selected = videos.get_selected_video(session, widget_id, source_id, "clip.mp4")
        page, cursor = videos.list_selected_videos(session, widget_id, limit=1)
        assert page == (selected,)
        assert cursor == first.id
        page, cursor = videos.list_selected_videos(
            session, widget_id, after_id=cursor, limit=1
        )
        assert page == (videos.SelectedVideo(source_id, "other.mkv", 123, -456),)
        assert cursor == last.id
        assert videos.list_selected_videos(session, widget_id, after_id=cursor) == (
            (),
            None,
        )
        for sid, path in [
            (source_id, "image.jpg"),
            (source_id, "absent.mp4"),
            (other, "clip.mp4"),
        ]:
            assert videos.get_selected_video(session, widget_id, sid, path) is None
        assert first.duration_ms is None
        assert first.display_width is first.display_height is first.orientation is None
    assert selected.identity == (source_id, "clip.mp4")
    assert (selected.size_bytes, selected.mtime_ns) == (123, -456)
    with pytest.raises(FrozenInstanceError):
        selected.relative_path = "changed.mp4"


@pytest.mark.parametrize(
    "changes",
    [
        {"source_id": 0},
        {"source_id": True},
        {"source_id": "1"},
        {"relative_path": "/clip.mp4"},
        {"relative_path": "../clip.mp4"},
        {"relative_path": "a//clip.mp4"},
        {"relative_path": ""},
        {"size_bytes": -1},
        {"size_bytes": True},
        {"mtime_ns": None},
        {"mtime_ns": True},
    ],
)
def test_invalid_snapshot(changes):
    with pytest.raises(ValueError):
        replace(videos.SelectedVideo(1, "clip.mp4", 0, -1), **changes)


@pytest.mark.parametrize(
    "source_id,path", [(True, "clip.mp4"), (0, "clip.mp4"), (1, "../clip.mp4")]
)
def test_invalid_lookup_identity(selection, source_id, path):
    db, _, widget_id = selection
    with db.transaction() as session, pytest.raises(ValueError):
        videos.get_selected_video(session, widget_id, source_id, path)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": 501},
        {"limit": True},
        {"after_id": -1},
        {"after_id": True},
    ],
)
def test_page_bounds(selection, kwargs):
    db, _, widget_id = selection
    with db.transaction() as session, pytest.raises(ValueError):
        videos.list_selected_videos(session, widget_id, **kwargs)
