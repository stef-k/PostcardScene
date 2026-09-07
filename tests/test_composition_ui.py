"""Composition HTTP contracts over real authentication, CSRF and persistence."""

import re

import pytest

from postcardscene import domain as d
from postcardscene.catalog import MediaCatalogState, MediaItem, get_media_item


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
def test_widget_create_and_fixed_kind_edit(composition, kind, monkeypatch):
    _, client, db, source_id, web_id, _, _, post = composition

    def forbidden(*args, **kwargs):
        pytest.fail("Composition management started external work")

    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
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
    expected_fields = {"name", "source_id", "enabled", "csrf_token"}
    if kind in ("image", "portrait_image_pair"):
        expected_fields.add("fit")
    elif kind == "video":
        expected_fields.update(("audio_enabled", "volume"))
    controls = re.findall(r'<(?:input|select|textarea)\b[^>]*\bname="([^"]+)"', html)
    assert set(controls) == expected_fields
    for field in expected_fields - {"csrf_token"}:
        assert f'for="{field}"' in html
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


@pytest.mark.parametrize(
    "field,value",
    [
        ("duration_seconds", "0"),
        ("duration_seconds", "86401"),
        ("duration_seconds", "1.5"),
        ("duration_seconds", "invalid"),
        ("widget_id", "9999"),
        ("widget_id", "invalid"),
    ],
)
def test_invalid_scene_edit_preserves_placements(composition, field, value):
    _, _, db, _, _, widget_id, scene_id, post = composition
    with db.transaction() as session:
        placement_id = d.list_scene_placements(session, scene_id)[0].id
    data = dict(name="Changed", widget_id=widget_id, duration_seconds="10")
    data[field] = value
    response = post(f"/scenes/{scene_id}/edit", **data)
    assert 'aria-invalid="true"' in response.text
    with db.transaction() as session:
        assert d.get_scene(session, scene_id).name == "Single"
        assert d.list_scene_placements(session, scene_id)[0].id == placement_id


def test_authenticated_pages_and_csrf_mutations(composition):
    app, client, db, _, _, widget_id, scene_id, _ = composition
    anonymous = app.test_client()
    token = re.search(
        r'name="csrf_token"[^>]*value="([^"]+)"', anonymous.get("/login").text
    )[1]
    forms = [f"/widgets/new/{kind}" for kind in d.WIDGET_KINDS] + [
        f"/widgets/{widget_id}/edit",
        "/scenes/new",
        f"/scenes/{scene_id}/edit",
    ]
    deletes = [f"/widgets/{widget_id}/delete", f"/scenes/{scene_id}/delete"]
    for path in ["/widgets", "/scenes", *forms]:
        response = anonymous.get(path)
        assert response.status_code == 302 and response.location.startswith("/login")
    for path in [*forms, *deletes]:
        response = anonymous.post(path, data={"csrf_token": token})
        assert response.status_code == 302 and response.location.startswith("/login")
        for data in ({}, {"csrf_token": "invalid"}):
            assert client.post(path, data=data).status_code == 400
    for path in deletes:
        assert client.get(path).status_code == 405
    with db.transaction() as session:
        assert len(d.list_widgets(session)) == len(d.list_scenes(session)) == 1
        assert d.get_widget(session, widget_id).enabled
        assert d.get_scene(session, scene_id).enabled


@pytest.mark.parametrize("volume", ["-1", "101", "1.5", "invalid", ""])
def test_invalid_video_configuration_is_not_created(composition, volume):
    _, _, db, source_id, _, _, _, post = composition
    response = post(
        "/widgets/new/video", name="Video", source_id=source_id, volume=volume
    )
    assert 'aria-invalid="true"' in response.text
    with db.transaction() as session:
        assert len(d.list_widgets(session)) == 1


def test_disabled_mounted_source_and_widget_reassignment(composition):
    _, client, db, source_id, _, widget_id, scene_id, post = composition
    with db.transaction() as session:
        mounted_id = d.create_source(
            session,
            name="Offline mount",
            kind="mounted_directory",
            enabled=False,
            configuration={"path": "/private/offline", "recursive": True},
        ).id
    response = post(
        "/widgets/new/video",
        name="Video",
        source_id=mounted_id,
        audio_enabled="y",
        volume="100",
    )
    assert "Widget saved." in response.text
    with db.transaction() as session:
        video = d.list_widgets(session)[-1]
        video_id = video.id
        assert not video.enabled
        assert video.configuration == {"audio_enabled": True, "volume": 100}
    html = client.get(f"/widgets/{video_id}/edit").text
    assert "Offline mount (mounted_directory) — Disabled" in html
    assert "/private/offline" not in html
    html = client.get(f"/scenes/{scene_id}/edit").text
    assert "Video (video) — Disabled" in html
    assert (
        "Scene saved."
        in post(f"/scenes/{scene_id}/edit", name="Reassigned", widget_id=video_id).text
    )
    with db.transaction() as session:
        assert d.list_scene_placements(session, scene_id)[0].widget_id == video_id
        assert d.get_widget(session, widget_id).source_id == source_id
        assert not d.get_source(session, mounted_id).enabled


@pytest.mark.parametrize(
    "layout,regions",
    [("split_vertical", ["left", "right"]), ("split_horizontal", ["top", "bottom"])],
)
def test_restrictive_lifecycle_and_split_preservation(composition, layout, regions):
    _, client, db, source_id, web_id, widget_id, scene_id, post = composition
    with db.transaction() as session:
        session.add(
            MediaItem(
                source_id=source_id,
                relative_path="retained.jpg",
                media_type="image",
                size_bytes=10,
                mtime_ns=20,
                seen_generation=1,
            )
        )
        session.add(MediaCatalogState(source_id=source_id, last_result="ready"))
        web_widget = d.create_widget(
            session, name="Web", kind="web_view", configuration={}, source_id=web_id
        ).id
        split_id = d.create_scene(
            session,
            name="Future",
            layout=layout,
            placements=list(zip(regions, [widget_id, web_widget])),
        ).id
        sequence_id = d.create_sequence(
            session,
            name="Sequence",
            mode="ordered",
            memberships=[(scene_id, None), (split_id, None)],
        ).id
        original = [
            (p.id, p.region, p.widget_id)
            for p in d.list_scene_placements(session, split_id)
        ]
    html = client.get("/scenes").text
    assert "Not executable in V0" in html and layout in html
    assert f'href="/scenes/{split_id}/edit"' not in html
    assert client.get(f"/scenes/{split_id}/edit").status_code == 409
    assert (
        post(
            f"/scenes/{split_id}/edit",
            name="Convert",
            layout="single",
            widget_id=widget_id,
        ).status_code
        == 409
    )
    assert "referenced by a Scene" in post(f"/widgets/{widget_id}/delete").text
    for referenced_id in (scene_id, split_id):
        assert (
            "referenced by a Sequence" in post(f"/scenes/{referenced_id}/delete").text
        )
    assert (
        "Widget saved."
        in post(
            f"/widgets/{widget_id}/edit",
            name="Disabled",
            source_id=source_id,
            fit="contain",
        ).text
    )
    with db.transaction() as session:
        assert d.get_scene(session, split_id).layout == layout
        assert [
            (p.id, p.region, p.widget_id)
            for p in d.list_scene_placements(session, split_id)
        ] == original
        assert d.get_scene(session, scene_id).enabled
        assert d.get_source(session, source_id).enabled
        assert len(d.list_sequence_memberships(session, sequence_id)) == 2
        d.remove_sequence(session, sequence_id)
    for removable_id in (split_id, scene_id):
        assert "Scene deleted." in post(f"/scenes/{removable_id}/delete").text
    assert "Widget deleted." in post(f"/widgets/{widget_id}/delete").text
    with db.transaction() as session:
        assert len(d.list_sources(session)) == 2
        assert d.get_widget(session, web_widget).source_id == web_id
        assert get_media_item(session, source_id, "retained.jpg").mtime_ns == 20
        assert session.get(MediaCatalogState, source_id).last_result == "ready"
        assert not session.get(MediaCatalogState, source_id).refresh_pending
        assert d.list_scenes(session) == []


@pytest.mark.parametrize("kind", ["image", "portrait_image_pair", "video", "web_view"])
def test_empty_sources_and_incompatible_selection(composition, kind):
    _, client, db, source_id, web_id, _, _, post = composition
    incompatible = source_id if kind == "web_view" else web_id
    response = post(
        f"/widgets/new/{kind}",
        name="Invalid",
        source_id=incompatible,
        fit="contain",
        volume="50",
    )
    assert 'aria-invalid="true"' in response.text
    with db.transaction() as session:
        for scene in d.list_scenes(session):
            d.remove_scene(session, scene.id)
        for widget in d.list_widgets(session):
            d.remove_widget(session, widget.id)
        for source in d.list_sources(session):
            d.remove_source(session, source.id)
    assert "No compatible Source exists" in client.get(f"/widgets/new/{kind}").text
    assert "No Widget exists" in client.get("/scenes/new").text
    assert (
        "Widget saved."
        not in post(
            f"/widgets/new/{kind}",
            name="Invalid",
            source_id=source_id,
            fit="contain",
            volume="50",
        ).text
    )
    assert "Scene saved." not in post("/scenes/new", name="Invalid", widget_id=999).text
    with db.transaction() as session:
        assert (
            d.list_sources(session)
            == d.list_widgets(session)
            == d.list_scenes(session)
            == []
        )


@pytest.mark.parametrize(
    "path", ["/widgets", "/scenes", "/widgets/new/video", "/scenes/new"]
)
def test_database_failure_is_sanitized(composition, path):
    _, client, db, _, _, _, _, _ = composition
    db.path.rename(db.path.with_suffix(".unavailable"))
    response = client.get(path)
    assert response.status_code == 503
    assert "Check database health" in response.text
    assert str(db.path) not in response.text
    assert "Traceback" not in response.text
