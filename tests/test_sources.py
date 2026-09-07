"""Source routes: authenticated structured configuration and failure boundaries."""

import re
from importlib import import_module

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from postcardscene import domain
from postcardscene.catalog import MediaCatalogState
from postcardscene.persistence import DatabaseError

sources_web = import_module("postcardscene.web.sources")


def test_authentication_csrf_and_private_authority(sources_ui):
    app, client, db, source_id, root = sources_ui
    anonymous = app.test_client()
    token = re.search(
        r'name="csrf_token"[^>]*value="([^"]+)"', anonymous.get("/login").text
    )[1]
    for path in ["/sources", "/sources/new", f"/sources/{source_id}/edit"]:
        response = anonymous.get(path)
        assert response.status_code == 302
        assert response.location.startswith("/login")
        assert str(root) not in response.text
    for path in [
        "/sources/new",
        *[f"/sources/{source_id}/{action}" for action in ("edit", "delete", "refresh")],
    ]:
        response = anonymous.post(path, data={"csrf_token": token})
        assert response.status_code == 302
        assert response.location.startswith("/login")
        assert str(root) not in response.text
        for csrf in (None, "invalid"):
            assert (
                client.post(path, data={"csrf_token": csrf} if csrf else {}).status_code
                == 400
            )
    with db.transaction() as session:
        assert len(domain.list_sources(session)) == 1
        assert session.get(MediaCatalogState, source_id) is None


@pytest.mark.parametrize("kind", ["local_directory", "mounted_directory"])
@pytest.mark.parametrize("enabled", [True, False])
def test_create_normalizes_and_queues_after_commit(
    sources_ui, source_post, monkeypatch, kind, enabled
):
    _, _, db, _, root = sources_ui
    from postcardscene.catalog_requests import request_catalog_reconciliation

    calls = []

    def queue(database, source_id):
        with db.transaction() as session:
            source = domain.get_source(session, source_id)
            assert source.configuration == {
                "path": str(root / "album"),
                "recursive": True,
            }
        calls.append(source_id)
        return request_catalog_reconciliation(database, source_id)

    def forbidden(*args, **kwargs):
        pytest.fail("Source request ran storage operations")

    monkeypatch.setattr(sources_web, "request_catalog_reconciliation", queue)
    monkeypatch.setattr(
        "postcardscene.catalog_reconciliation.reconcile_filesystem_source", forbidden
    )
    monkeypatch.setattr("postcardscene.filesystem_source.source_status", forbidden)
    monkeypatch.setattr("postcardscene.mounted_source.mounted_source_status", forbidden)
    response = source_post(
        kind=kind, path=str(root) + "//./album/", enabled="y" if enabled else None
    )
    assert response.status_code == 200
    assert "Source saved." in response.text
    with db.transaction() as session:
        source = domain.list_sources(session)[-1]
        assert source.name == "Library" and source.kind == kind
        assert source.enabled == enabled
        state = session.get(MediaCatalogState, source.id)
        if enabled:
            assert calls == [source.id]
            assert state.requested_generation == 1
            assert "Catalog refresh queued" in response.text
        else:
            assert calls == [] and state is None


@pytest.mark.parametrize(
    "bad_path", ["relative", "/outside/authority", "escape", "parent"]
)
def test_invalid_path_does_not_mutate(sources_ui, source_post, bad_path):
    _, _, db, source_id, root = sources_ui
    if bad_path == "escape":
        (root / "escape").symlink_to(root.parent, target_is_directory=True)
        bad_path = str(root / "escape" / "missing")
    elif bad_path == "parent":
        bad_path = str(root) + "/../outside"
    for path in ("/sources/new", f"/sources/{source_id}/edit"):
        response = source_post(path, path=bad_path)
        assert response.status_code == 200
        assert 'aria-invalid="true"' in response.text
        assert "within an allowed root" in response.text
    with db.transaction() as session:
        assert len(domain.list_sources(session)) == 1
        source = domain.get_source(session, source_id)
        assert source.name == "Photos"
        assert source.configuration["path"] == str(root)
        assert session.get(MediaCatalogState, source_id) is None


@pytest.mark.parametrize("roots", [[], ["/"], "invalid"])
def test_empty_or_misconfigured_authority_is_read_only_and_fails_closed(
    sources_ui, source_post, roots
):
    app, client, db, source_id, root = sources_ui
    app.config["MEDIA_ALLOWED_ROOTS"] = roots
    assert client.get("/sources").status_code == 200
    for path in ("/sources/new", f"/sources/{source_id}/edit"):
        response = source_post(path, path=str(root / "new"))
        assert response.status_code == 200
        assert "No allowed media roots are configured" in response.text
    with db.transaction() as session:
        assert len(domain.list_sources(session)) == 1
        assert domain.get_source(session, source_id).configuration["path"] == str(root)
    assert app.config["MEDIA_ALLOWED_ROOTS"] == roots


def test_structured_accessible_form_and_sources_navigation(sources_ui):
    _, client, _, source_id, root = sources_ui
    html = client.get(f"/sources/{source_id}/edit").text
    for field in ("name", "kind", "path", "recursive", "enabled"):
        assert f'for="{field}"' in html
        assert f'name="{field}"' in html
    for text in (
        str(root),
        "read-only host configuration",
        "already-mounted Linux NFS/SMB",
        "does not mount shares",
        "collect NAS credentials",
        "mount/unmount commands",
        'class="form-check-input"',
        'class="text-break"',
        'class="col-lg-9 col-xl-8"',
        'name="csrf_token"',
        'aria-describedby="delete-warning"',
        "Widgets, Scenes and Sequences are never deleted",
    ):
        assert text in html
    controls = re.findall(r'<(?:input|select|textarea)\b[^>]*\bname="([^"]+)"', html)
    assert set(controls) == {
        "name",
        "kind",
        "path",
        "recursive",
        "enabled",
        "csrf_token",
    }
    assert html.count('id="csrf_token"') == 1
    assert 'href="/sources" aria-current="page"' in html
    assert 'href="/sources"' in client.get("/").text
    assert client.get(f"/sources/{source_id}/delete").status_code == 405
    assert client.get(f"/sources/{source_id}/refresh").status_code == 405


@pytest.mark.parametrize(
    "fields", [{"kind": "web_url"}, {"name": "   "}, {"name": "x" * 129}, {"path": ""}]
)
def test_invalid_fields_are_form_feedback(sources_ui, source_post, fields):
    _, _, db, _, _ = sources_ui
    response = source_post(**fields)
    assert response.status_code == 200
    assert 'aria-invalid="true"' in response.text
    with db.transaction() as session:
        assert len(domain.list_sources(session)) == 1


def test_missing_sources_cannot_be_managed(sources_ui, source_post):
    _, client, _, _, _ = sources_ui
    assert client.get("/sources/999/edit").status_code == 404
    for action in ("edit", "delete"):
        assert source_post(f"/sources/999/{action}").status_code == 404
    assert "Refresh could not be queued" in source_post("/sources/999/refresh").text


@pytest.mark.parametrize("exception", [DatabaseError, SQLAlchemyError])
def test_queue_failure_retains_saved_source_and_can_retry(
    sources_ui, source_post, monkeypatch, exception
):
    _, _, db, _, _ = sources_ui
    with monkeypatch.context() as patch:

        def fail(*args):
            raise exception("private SQL /secret/db")

        patch.setattr(sources_web, "request_catalog_reconciliation", fail)
        response = source_post()
    assert "Source saved, but catalog refresh could not be queued" in response.text
    assert "private SQL" not in response.text
    with db.transaction() as session:
        source_id = session.scalar(
            select(domain.Source.id).where(domain.Source.name == "Library")
        )
        assert source_id
        assert session.get(MediaCatalogState, source_id) is None
    assert "Catalog refresh queued" in source_post(f"/sources/{source_id}/refresh").text


@pytest.mark.parametrize("exception", [DatabaseError, SQLAlchemyError])
def test_database_failure_is_sanitized_even_during_login_lookup(
    sources_ui, source_post, monkeypatch, exception
):
    app, client, _, source_id, root = sources_ui

    def fail(*args, **kwargs):
        raise exception(f"private SQL {root}")

    monkeypatch.setattr(app.extensions["postcardscene.database"], "transaction", fail)
    for path in ("/sources", "/sources/new", f"/sources/{source_id}/edit"):
        response = client.get(path)
        assert response.status_code == 503
        assert "Check database health" in response.text
        assert str(root) not in response.text
    for path in (
        "/sources/new",
        *[f"/sources/{source_id}/{action}" for action in ("edit", "delete", "refresh")],
    ):
        response = source_post(path)
        assert response.status_code == 503
        assert "private SQL" not in response.text


def test_failed_source_transaction_rolls_back_without_queuing(
    sources_ui, source_post, monkeypatch
):
    _, _, db, _, _ = sources_ui

    def fail_after_insert(session, **fields):
        domain.create_source(session, **fields)
        raise SQLAlchemyError("private insert failure")

    def forbidden(*args):
        pytest.fail("Queued refresh before a successful Source commit")

    monkeypatch.setattr(sources_web, "create_source", fail_after_insert)
    monkeypatch.setattr(sources_web, "request_catalog_reconciliation", forbidden)
    response = source_post()
    assert response.status_code == 503
    assert "private insert failure" not in response.text
    with db.transaction() as session:
        assert len(domain.list_sources(session)) == 1
        assert session.scalars(select(MediaCatalogState)).all() == []
