"""Image semantics and DB-only eligibility; no renderer or filesystem fixture data."""

from dataclasses import FrozenInstanceError, replace

import pytest

from postcardscene import domain as d
from postcardscene import image_selection as images
from postcardscene.catalog import MediaItem


@pytest.mark.parametrize("kind", ["image", "portrait_image_pair"])
@pytest.mark.parametrize(
    "configuration,fit",
    [({}, "contain"), ({"fit": "contain"}, "contain"), ({"fit": "cover"}, "cover")],
)
def test_configuration(kind, configuration, fit):
    assert images.validate_image_configuration(kind, configuration) == {"fit": fit}


@pytest.mark.parametrize(
    "configuration",
    [None, [], {"duration": 30}, {"fit": "fill"}, {"fit": None}, {"fit": []}],
)
def test_invalid_configuration(configuration):
    with pytest.raises(images.ImageSelectionError):
        images.validate_image_configuration("image", configuration)


@pytest.fixture
def selection(catalog):
    db, source_id, _, _ = catalog
    with db.transaction() as session:
        widget_id = d.create_widget(
            session, name="Image", kind="image", configuration={}, source_id=source_id
        ).id
    return db, source_id, widget_id


def add_item(session, source_id, path="photo.tiff", **changes):
    fields = dict(
        source_id=source_id,
        relative_path=path,
        media_type="image",
        size_bytes=123,
        mtime_ns=-456,
        seen_generation=1,
        display_width=10,
        display_height=20,
        orientation="portrait",
        metadata_status="ready",
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
            widget.kind = "video"
        elif problem == "no_source":
            widget.source_id = None
        elif problem == "missing_source":
            widget.source_id = source_id + 100
        elif problem == "disabled_source":
            source.enabled = False
        else:
            source.kind = "web_url"
        for query in (
            lambda: images.resolve_image_context(session, widget_id),
            lambda: images.get_selected_image(
                session, widget_id, source_id, "photo.tiff"
            ),
            lambda: images.list_selected_images(session, widget_id),
        ):
            with pytest.raises(images.ImageSelectionError):
                query()
        session.rollback()  # Deliberately invalid reference is never persisted.


@pytest.mark.parametrize("kind", ["local_directory", "mounted_directory"])
def test_selection_snapshot_and_keyset_pages(selection, kind):
    db, source_id, widget_id = selection
    with db.transaction() as session:
        d.get_source(session, source_id).kind = kind
        first = add_item(session, source_id)
        add_item(session, source_id, "video.mp4", media_type="video")
        last = add_item(session, source_id, "last.jpg")
        context = images.resolve_image_context(session, widget_id)
        assert (context.source_id, context.widget_kind, context.fit) == (
            source_id,
            "image",
            "contain",
        )
        selected = images.get_selected_image(
            session, widget_id, source_id, first.relative_path
        )
        page, continuation = images.list_selected_images(session, widget_id, limit=1)
        assert page == (selected,)
        assert continuation == first.id
        page, continuation = images.list_selected_images(
            session, widget_id, after_id=continuation, limit=1
        )
        assert [item.relative_path for item in page] == ["last.jpg"]
        assert continuation == last.id
        assert images.list_selected_images(
            session, widget_id, after_id=continuation
        ) == ((), None)
        assert (
            images.get_selected_image(session, widget_id, source_id, "absent.jpg")
            is None
        )
    assert selected.identity == (source_id, "photo.tiff")
    assert (
        selected.size_bytes,
        selected.mtime_ns,
        selected.display_width,
        selected.display_height,
        selected.orientation,
    ) == (123, -456, 10, 20, "portrait")
    with pytest.raises(FrozenInstanceError):
        selected.relative_path = "changed.jpg"


@pytest.mark.parametrize(
    "changes",
    [
        {"media_type": "video"},
        {"metadata_status": "pending"},
        {"metadata_status": "error"},
        {"display_width": 0},
        {"display_height": -1},
        {"display_width": None},
        {"orientation": "diagonal"},
        {"orientation": None},
    ],
)
def test_ineligible_items_excluded_from_lookup_and_page(selection, changes):
    db, source_id, widget_id = selection
    with db.transaction() as session:
        add_item(session, source_id, **changes)
        assert (
            images.get_selected_image(session, widget_id, source_id, "photo.tiff")
            is None
        )
        assert images.list_selected_images(session, widget_id) == ((), None)


def test_other_source_is_not_selectable(selection):
    db, source_id, widget_id = selection
    with db.transaction() as session:
        other = d.create_source(
            session, name="Other", kind="local_directory", configuration={}
        ).id
        add_item(session, other)
        assert (
            images.get_selected_image(session, widget_id, other, "photo.tiff") is None
        )
        assert (
            images.get_selected_image(session, widget_id, source_id, "photo.tiff")
            is None
        )
        assert images.list_selected_images(session, widget_id) == ((), None)


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
        images.list_selected_images(session, widget_id, **kwargs)


def selected(path="first.jpg", orientation="portrait"):
    width, height = {"portrait": (10, 20), "landscape": (20, 10), "square": (10, 10)}[
        orientation
    ]
    return images.SelectedImage(1, path, 123, 456, width, height, orientation)


@pytest.mark.parametrize(
    "kind,first_orientation,next_orientation,consumed",
    [
        ("image", "portrait", "portrait", 1),
        ("portrait_image_pair", "portrait", "portrait", 2),
        ("portrait_image_pair", "portrait", "landscape", 1),
        ("portrait_image_pair", "landscape", "portrait", 1),
        ("portrait_image_pair", "square", "portrait", 1),
        ("portrait_image_pair", "portrait", None, 1),
    ],
)
def test_grouping(kind, first_orientation, next_orientation, consumed):
    first = selected(orientation=first_orientation)
    lookahead = selected("next.jpg", next_orientation) if next_orientation else None
    result = images.build_image_frame(kind, "cover", first, lookahead)
    frame, count = result
    assert count == consumed
    assert frame.images == ((first, lookahead) if consumed == 2 else (first,))
    assert frame.fit == "cover"
    assert images.build_image_frame(kind, "cover", first, lookahead) == result
    with pytest.raises(FrozenInstanceError):
        frame.fit = "contain"


def test_identity_not_freshness_determines_duplicate():
    first = selected()
    duplicate = replace(first, mtime_ns=999)
    frame, consumed = images.build_image_frame(
        "portrait_image_pair", "contain", first, duplicate
    )
    assert frame.images == (first,)
    assert consumed == 1
    other_source = replace(first, source_id=2)
    assert (
        images.build_image_frame("portrait_image_pair", "contain", first, other_source)[
            1
        ]
        == 2
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"display_width": 0},
        {"display_height": True},
        {"orientation": "bad"},
        {"relative_path": "/absolute.jpg"},
        {"source_id": 0},
        {"size_bytes": -1},
        {"mtime_ns": None},
    ],
)
def test_invalid_snapshot(changes):
    with pytest.raises(ValueError):
        replace(selected(), **changes)


def test_grouping_rejects_invalid_semantics():
    with pytest.raises(images.ImageSelectionError):
        images.build_image_frame("video", "contain", selected())
    with pytest.raises(images.ImageSelectionError):
        images.build_image_frame("image", "fill", selected())


def test_pages_merge_orientations_without_skips_or_duplicates(selection):
    db, source_id, widget_id = selection
    with db.transaction() as session:
        expected = []
        for index, orientation in enumerate(["portrait", "square", "landscape"] * 3):
            snapshot = selected(f"{index}.jpg", orientation)
            item = add_item(
                session,
                source_id,
                snapshot.relative_path,
                display_width=snapshot.display_width,
                display_height=snapshot.display_height,
                orientation=orientation,
            )
            expected.append(item.identity)
        observed = []
        continuation = 0
        while True:
            page, continuation = images.list_selected_images(
                session, widget_id, after_id=continuation, limit=2
            )
            if not page:
                break
            assert len(page) <= 2
            observed.extend(image.identity for image in page)
        assert observed == expected


def test_invalid_lookahead_remains_unconsumed():
    first = selected()
    frame, count = images.build_image_frame(
        "portrait_image_pair", "contain", first, object()
    )
    assert frame.images == (first,)
    assert count == 1
