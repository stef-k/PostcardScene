"""Authenticated web Source management preserves filesystem/catalog boundaries."""

import re
from importlib import import_module

import pytest
from sqlalchemy import select

from postcardscene import domain as d
from postcardscene.catalog import MediaCatalogState, MediaItem


@pytest.fixture
def web_source(sources_ui):
    app, client, db, _, _ = sources_ui
    with db.transaction() as session:
        source_id = d.create_source(
            session,
            name="Web",
            kind="web_url",
            configuration={"url": "https://host/PrivateShare"},
        ).id
    return app, client, db, source_id


def test_private_pages_and_csrf(web_source):
    app, client, db, source_id = web_source
    anonymous = app.test_client()
    token = re.search(
        r'name="csrf_token"[^>]*value="([^"]+)"', anonymous.get("/login").text
    )[1]
    for path in ("/sources", "/sources/new/web", f"/sources/{source_id}/edit"):
        response = anonymous.get(path)
        assert response.status_code == 302 and response.location.startswith("/login")
        assert "PrivateShare" not in response.text
    for path in (
        "/sources/new/web",
        f"/sources/{source_id}/edit",
        f"/sources/{source_id}/delete",
    ):
        response = anonymous.post(path, data={"csrf_token": token})
        assert response.status_code == 302 and response.location.startswith("/login")
        assert "PrivateShare" not in response.text
        for csrf in (None, "invalid"):
            assert (
                client.post(path, data={"csrf_token": csrf} if csrf else {}).status_code
                == 400
            )
    with db.transaction() as session:
        assert d.get_source(session, source_id).enabled


def test_create_edit_disable_and_no_catalog_or_probes(
    web_source, source_post, monkeypatch
):
    app, client, db, source_id = web_source
    app.config["MEDIA_ALLOWED_ROOTS"] = []

    def forbidden(*args, **kwargs):
        pytest.fail("Web Source management performed network, browser or catalog work")

    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr(
        import_module("postcardscene.web.sources"),
        "request_catalog_reconciliation",
        forbidden,
    )
    url = "http://wayfarer.local:8080/Case%2Fpath?share=private#view"
    response = source_post("/sources/new/web", kind="web_url", url="  " + url + "  ")
    assert "Source saved." in response.text
    with db.transaction() as session:
        created = d.list_sources(session)[-1]
        assert created.configuration == {"url": url}
        assert d.list_widgets(session) == []
    for enabled in (None, "y"):
        response = source_post(
            f"/sources/{source_id}/edit", kind="web_url", url=url, enabled=enabled
        )
        assert "Source saved." in response.text
        with db.transaction() as session:
            source = d.get_source(session, source_id)
            assert source.enabled == bool(enabled)
            assert source.configuration == {"url": url}
            assert session.scalars(select(MediaCatalogState)).all() == []
            assert session.scalars(select(MediaItem)).all() == []
    html = client.get(f"/sources/{source_id}/edit").text
    controls = re.findall(r'<(?:input|select|textarea)\b[^>]*\bname="([^"]+)"', html)
    assert set(controls) == {"name", "kind", "url", "enabled", "csrf_token"}
    for field in ("name", "kind", "url", "enabled"):
        assert f'for="{field}"' in html
    listing = client.get("/sources").text
    card = listing.split(f'id="source-{source_id}"')[1].split("</section>")[0]
    assert url.replace("&", "&amp;") in card
    assert "Refresh" not in card and "catalog" not in card


@pytest.mark.parametrize(
    "url",
    [
        "file:///private",
        "http://user:secret@host",
        "http://host:0",
        "http://host/a b",
        "x" * 8193,
    ],
)
def test_invalid_url_preserves_state(web_source, source_post, url):
    _, _, db, source_id = web_source
    for route in ("/sources/new/web", f"/sources/{source_id}/edit"):
        response = source_post(route, kind="web_url", url=url)
        assert 'aria-invalid="true"' in response.text
    with db.transaction() as session:
        assert len(d.list_sources(session)) == 2
        assert d.get_source(session, source_id).configuration == {
            "url": "https://host/PrivateShare"
        }


def test_reference_delete_and_refresh(web_source, source_post):
    _, _, db, source_id = web_source
    with db.transaction() as session:
        widget_id = d.create_widget(
            session, name="Web", kind="web_view", configuration={}, source_id=source_id
        ).id
    assert "referenced by a Widget" in source_post(f"/sources/{source_id}/delete").text
    assert (
        "Refresh could not be queued"
        in source_post(f"/sources/{source_id}/refresh").text
    )
    with db.transaction() as session:
        assert d.get_source(session, source_id).configuration == {
            "url": "https://host/PrivateShare"
        }
        assert session.get(MediaCatalogState, source_id) is None
        d.remove_widget(session, widget_id)
    assert "deleted" in source_post(f"/sources/{source_id}/delete").text
    with db.transaction() as session:
        assert session.get(d.Source, source_id) is None
        assert len(d.list_sources(session)) == 1
