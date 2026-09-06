"""Source lifecycle and persisted health through the authenticated control plane."""

from importlib import import_module

import pytest
from sqlalchemy import event, select

from postcardscene import domain
from postcardscene.catalog import MediaCatalogState, MediaItem, catalog_health_counts
from postcardscene.settings import set_timezone


@pytest.fixture
def populated(sources_ui):
    _, _, db, source_id, _ = sources_ui
    with db.transaction() as session:
        session.add(
            MediaCatalogState(
                source_id=source_id,
                scan_generation=4,
                completed_generation=3,
                requested_generation=7,
                handled_request_generation=5,
                last_result="ready",
                last_attempt_ns=1_700_000_000_000_000_000,
                last_success_ns=1_600_000_000_000_000_000,
            )
        )
        for index, (kind, status) in enumerate(
            (
                ("image", "ready"),
                ("image", "pending"),
                ("image", "error"),
                ("video", None),
            )
        ):
            session.add(
                MediaItem(
                    source_id=source_id,
                    relative_path=f"item{index}",
                    media_type=kind,
                    size_bytes=10,
                    mtime_ns=1,
                    seen_generation=3,
                    metadata_status=status,
                )
            )
    return source_id


def catalog_snapshot(db, source_id):
    with db.transaction() as session:
        state = session.get(MediaCatalogState, source_id)
        return (
            {
                column.name: getattr(state, column.name)
                for column in MediaCatalogState.__table__.columns
            },
            [
                (item.id, item.relative_path, item.metadata_status)
                for item in session.scalars(
                    select(MediaItem).where(MediaItem.source_id == source_id)
                )
            ],
        )


def test_rename_preserves_every_catalog_field_and_normalized_path(
    sources_ui, source_post, populated
):
    _, _, db, source_id, root = sources_ui
    before = catalog_snapshot(db, source_id)
    response = source_post(
        f"/sources/{source_id}/edit", name="New name", path=str(root) + "/./"
    )
    assert "Source saved" in response.text
    assert "Catalog refresh queued" not in response.text
    assert catalog_snapshot(db, source_id) == before
    with db.transaction() as session:
        assert domain.get_source(session, source_id).name == "New name"


@pytest.mark.parametrize("change", ["path", "kind", "recursive"])
def test_authority_edit_invalidates_and_requests_once(
    sources_ui, source_post, populated, change
):
    _, _, db, source_id, root = sources_ui
    changes = {
        "path": str(root / "new"),
        "kind": "mounted_directory",
        "recursive": None,
    }
    response = source_post(f"/sources/{source_id}/edit", **{change: changes[change]})
    assert "Catalog refresh queued" in response.text
    state, items = catalog_snapshot(db, source_id)
    assert items == []
    assert state["scan_generation"] == state["completed_generation"] == 5
    assert (
        state["requested_generation"] == 8 and state["handled_request_generation"] == 7
    )
    assert state["last_result"] == "never_scanned"
    assert state["last_attempt_ns"] is state["last_success_ns"] is None


def test_disable_preserves_knowledge_supersedes_work_and_reenable_queues(
    sources_ui, source_post, populated
):
    _, _, db, source_id, _ = sources_ui
    before, items = catalog_snapshot(db, source_id)
    response = source_post(f"/sources/{source_id}/edit", enabled=None)
    state, retained = catalog_snapshot(db, source_id)
    assert "Disabled" in response.text
    assert "Catalog refresh queued" not in response.text
    assert retained == items
    for key in ("last_result", "last_attempt_ns", "last_success_ns"):
        assert state[key] == before[key]
    assert state["scan_generation"] == state["completed_generation"] == 5
    assert state["requested_generation"] == state["handled_request_generation"] == 7
    assert (
        "Refresh could not be queued"
        in source_post(f"/sources/{source_id}/refresh").text
    )
    assert catalog_snapshot(db, source_id)[0] == state
    response = source_post(f"/sources/{source_id}/edit")
    assert "Catalog refresh queued" in response.text
    refreshed, retained = catalog_snapshot(db, source_id)
    assert refreshed == dict(state, requested_generation=8)
    assert retained == items


def test_delete_respects_composition_and_cascades_only_owned_state(
    sources_ui, source_post, populated
):
    _, _, db, source_id, root = sources_ui
    original = root / "original.jpg"
    original.write_bytes(b"external media")
    with db.transaction() as session:
        other_id = domain.create_source(
            session,
            name="Other",
            kind="local_directory",
            configuration={"path": str(root), "recursive": True},
        ).id
        widget_id = domain.create_widget(
            session, name="Photo", kind="image", configuration={}, source_id=source_id
        ).id
        scene_id = domain.create_scene(
            session, name="Scene", layout="single", placements=[("main", widget_id)]
        ).id
        sequence_id = domain.create_sequence(
            session, name="Sequence", mode="ordered", memberships=[(scene_id, None)]
        ).id
        session.add(MediaCatalogState(source_id=other_id))
    before = catalog_snapshot(db, source_id)
    assert "referenced by a Widget" in source_post(f"/sources/{source_id}/delete").text
    assert catalog_snapshot(db, source_id) == before
    with db.transaction() as session:
        domain.update_widget(
            session,
            widget_id,
            name="Photo",
            kind="image",
            configuration={},
            enabled=True,
            source_id=other_id,
        )
    assert (
        "Source and its derived catalog deleted"
        in source_post(f"/sources/{source_id}/delete").text
    )
    with db.transaction() as session:
        assert session.get(domain.Source, source_id) is None
        assert session.get(MediaCatalogState, source_id) is None
        assert catalog_health_counts(session, source_id) == {}
        assert domain.get_widget(session, widget_id).source_id == other_id
        assert domain.get_scene(session, scene_id).name == "Scene"
        assert domain.get_sequence(session, sequence_id).name == "Sequence"
        assert domain.list_scene_placements(session, scene_id)[0].widget_id == widget_id
        assert (
            domain.list_sequence_memberships(session, sequence_id)[0].scene_id
            == scene_id
        )
        assert session.get(MediaCatalogState, other_id) is not None
    assert original.read_bytes() == b"external media"


def forbid_storage(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Control request touched filesystem/catalog enumeration")

    for module_name, names in {
        "postcardscene.filesystem_source": (
            "source_status",
            "enumerate_local_directory",
            "resolve_item_path",
            "validate_source",
        ),
        "postcardscene.mounted_source": (
            "mounted_source_status",
            "enumerate_mounted_directory",
        ),
        "postcardscene.catalog_reconciliation": (
            "reconcile_filesystem_source",
            "enumerate_local_directory",
            "enumerate_mounted_directory",
        ),
        "postcardscene.web.sources": ("validate_source", "PathPolicy"),
        "postcardscene.catalog": ("list_media_items",),
        "PIL.Image": ("open",),
        "os": ("scandir",),
    }.items():
        module = import_module(module_name)
        for name in names:
            monkeypatch.setattr(module, name, forbidden)


@pytest.mark.parametrize(
    "result,scan,completed,requested,handled,enabled,label",
    [
        ("never_scanned", 0, 0, 0, 0, True, "Never scanned"),
        ("ready", 4, 4, 8, 7, True, "Refresh queued"),
        ("ready", 4, 3, 8, 7, True, "Refreshing / interrupted"),
        ("ready", 4, 4, 7, 7, True, "Ready"),
        ("unavailable", 4, 4, 7, 7, True, "Source unavailable"),
        ("error", 4, 4, 7, 7, True, "Needs attention"),
        ("cancelled", 4, 4, 7, 7, True, "Cancelled"),
        ("unavailable", 4, 3, 8, 7, False, "Disabled"),
    ],
)
def test_health_is_db_only_with_bounded_counts(
    sources_ui,
    populated,
    monkeypatch,
    result,
    scan,
    completed,
    requested,
    handled,
    enabled,
    label,
):
    app, client, db, source_id, _ = sources_ui
    set_timezone(db, "Europe/Athens")
    with db.transaction() as session:
        source = domain.get_source(session, source_id)
        source.kind = "mounted_directory"
        source.enabled = enabled
        state = session.get(MediaCatalogState, source_id)
        state.last_result = result
        state.scan_generation, state.completed_generation = scan, completed
        state.requested_generation, state.handled_request_generation = (
            requested,
            handled,
        )
    queries = []

    def record(connection, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    engine = app.extensions["postcardscene.database"].engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        with monkeypatch.context() as patch:
            forbid_storage(patch)
            response = client.get("/sources")
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200
    assert f'<h3 class="h5">{label}</h3>' in response.text
    assert "Last recorded result:" in response.text
    assert "2023-11-15 00:13:20 EET (UTC+0200)" in response.text
    assert "2020-09-13 15:26:40 EEST (UTC+0300)" in response.text
    for name, count in (
        ("Total cataloged items", 4),
        ("Images total", 3),
        ("Image metadata ready", 1),
        ("Image metadata pending", 1),
        ("Image metadata error", 1),
        ("Videos total", 1),
    ):
        assert f'{name}</dt><dd class="col-3">{count}</dd>' in response.text
    assert "not confirmation that retained files are currently online" in response.text
    assert "outage does not mean confirmed deletion" in response.text
    catalog_queries = [query for query in queries if "FROM media_item" in query]
    assert len(catalog_queries) == 1
    assert "count(" in catalog_queries[0] and "GROUP BY" in catalog_queries[0]
    assert "media_item.relative_path" not in catalog_queries[0]


def test_manual_refresh_coalesces_without_runtime_or_storage(
    sources_ui, source_post, monkeypatch
):
    _, client, db, source_id, _ = sources_ui
    assert '<h3 class="h5">Never scanned</h3>' in client.get("/sources").text
    with monkeypatch.context() as patch:
        forbid_storage(patch)
        for generation in (1, 2):
            response = source_post(f"/sources/{source_id}/refresh")
            assert response.status_code == 200
            assert "Catalog refresh queued" in response.text
            assert '<h3 class="h5">Refresh queued</h3>' in response.text
            with db.transaction() as session:
                state = session.get(MediaCatalogState, source_id)
                assert state.requested_generation == generation
                assert state.handled_request_generation == state.scan_generation == 0
                assert state.last_attempt_ns is state.last_success_ns is None
