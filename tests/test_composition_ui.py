"""Composition HTTP contracts over real authentication, CSRF and persistence."""

import re

import pytest

from postcardscene import domain as d


@pytest.fixture
def composition(sources_ui):
    app, client, db, source_id, _ = sources_ui
    with db.transaction() as session:
        web_id = d.create_source(
            session,
            name="Private web",
            kind="web_url",
            enabled=False,
            configuration={"url": "https://host/PrivateShare"},
        ).id
        widget_id = d.create_widget(
            session,
            name="Photo",
            kind="image",
            configuration={"fit": "contain"},
            source_id=source_id,
        ).id
        scene_id = d.create_scene(
            session,
            name="Single",
            layout="single",
            placements=[("main", widget_id)],
        ).id
    token = re.search(
        r'name="csrf_token"[^>]*value="([^"]+)"', client.get("/sources/new").text
    )[1]

    def post(path, **data):
        return client.post(
            path, data={"csrf_token": token, **data}, follow_redirects=True
        )

    return app, client, db, source_id, web_id, widget_id, scene_id, post


@pytest.mark.parametrize("kind", ["image", "portrait_image_pair", "video", "web_view"])
def test_widget_create_and_fixed_kind_edit(composition, kind):
    _, client, db, source_id, web_id, _, _, post = composition
    source_id = web_id if kind == "web_view" else source_id
    response = post(
        f"/widgets/new/{kind}",
        name="  New  ",
        source_id=source_id,
        fit="cover",
        volume="50",
        enabled="y",
    )
    assert "Widget saved." in response.text
    expected = {"fit": "cover"}
    if kind == "video":
        expected = {"audio_enabled": False, "volume": 50}
    elif kind == "web_view":
        expected = {}
    with db.transaction() as session:
        widget = d.list_widgets(session)[-1]
        widget_id = widget.id
        assert (widget.name, widget.kind, widget.source_id) == ("New", kind, source_id)
        assert widget.configuration == expected
        assert len(d.list_sources(session)) == 2
    response = post(
        f"/widgets/{widget_id}/edit",
        name="Disabled",
        source_id=source_id,
        fit="contain",
        volume="0",
        kind="video" if kind != "video" else "image",
    )
    assert "Widget saved." in response.text
    with db.transaction() as session:
        widget = d.get_widget(session, widget_id)
        assert widget.kind == kind and not widget.enabled
        assert widget.name == "Disabled"
        assert d.get_source(session, source_id).enabled == (kind != "web_view")
    html = client.get(f"/widgets/{widget_id}/edit").text
    assert 'name="kind"' not in html
    assert "PrivateShare" not in html


@pytest.mark.parametrize(
    "field,value",
    [("fit", "stretch"), ("source_id", "9999"), ("source_id", "web"), ("name", " ")],
)
def test_invalid_widget_edit_preserves_state(composition, field, value):
    _, _, db, source_id, web_id, widget_id, _, post = composition
    data = dict(name="Changed", source_id=source_id, fit="cover")
    data[field] = web_id if value == "web" else value
    response = post(f"/widgets/{widget_id}/edit", **data)
    assert 'aria-invalid="true"' in response.text
    with db.transaction() as session:
        widget = d.get_widget(session, widget_id)
        assert (widget.name, widget.configuration, widget.enabled) == (
            "Photo",
            {"fit": "contain"},
            True,
        )


@pytest.mark.parametrize("duration", ["", "1", "86400"])
def test_single_scene_replacement_and_duration(composition, duration):
    _, _, db, _, _, widget_id, scene_id, post = composition
    for path in ("/scenes/new", f"/scenes/{scene_id}/edit"):
        response = post(
            path,
            name="Changed",
            widget_id=widget_id,
            duration_seconds=duration,
            layout="split_vertical",
        )
        assert "Scene saved." in response.text
    with db.transaction() as session:
        for scene in d.list_scenes(session):
            assert scene.layout == "single" and not scene.enabled
            assert scene.duration_seconds == (int(duration) if duration else None)
            assert [
                (p.region, p.widget_id, p.position)
                for p in d.list_scene_placements(session, scene.id)
            ] == [("main", widget_id, 0)]
        assert d.get_widget(session, widget_id).enabled


@pytest.mark.parametrize("duration", ["0", "86401", "1.5", "invalid"])
def test_invalid_scene_duration_preserves_placements(composition, duration):
    _, _, db, _, _, widget_id, scene_id, post = composition
    with db.transaction() as session:
        placement_id = d.list_scene_placements(session, scene_id)[0].id
    response = post(
        f"/scenes/{scene_id}/edit",
        name="Changed",
        widget_id=widget_id,
        duration_seconds=duration,
    )
    assert 'aria-invalid="true"' in response.text
    with db.transaction() as session:
        assert d.get_scene(session, scene_id).name == "Single"
        assert d.list_scene_placements(session, scene_id)[0].id == placement_id
