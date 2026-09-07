"""Sequence configuration HTTP behavior over real login, CSRF and SQLite."""

import html
import re

import pytest

from postcardscene import domain as d
from postcardscene.settings import get_playback_settings


@pytest.fixture
def sequences_ui(sources_ui):
    app, client, db, source_id, _ = sources_ui
    with db.transaction() as session:
        widget = d.create_widget(
            session,
            name="Photo",
            kind="image",
            source_id=source_id,
            configuration={"fit": "contain"},
        )
        first = d.create_scene(
            session, name="First", layout="single", placements=[("main", widget.id)]
        ).id
        second = d.create_scene(
            session,
            name="Second disabled",
            layout="single",
            placements=[("main", widget.id)],
            enabled=False,
        ).id
        sequence = d.create_sequence(
            session,
            name="Original",
            mode="ordered",
            memberships=[(first, None), (second, 1), (first, 86400)],
        ).id
    token = re.search(
        r'name="csrf_token"[^>]*value="([^"]+)"', client.get("/settings").text
    )[1]

    def post(path, **data):
        return client.post(
            path, data={"csrf_token": token, **data}, follow_redirects=True
        )

    return app, client, db, first, second, sequence, post


def draft_fields(response):
    """Submit the rendered draft as a browser would, including selected options."""
    markup = re.search(
        r'<form method="post" class="card mb-4">(.*?)</form>', response.text, re.S
    )[1]
    data = {}
    for tag in re.findall(r"<input\b[^>]*>", markup):
        if 'type="checkbox"' in tag and "checked" not in tag:
            continue
        name = re.search(r'name="([^"]+)"', tag)[1]
        value = re.search(r'value="([^"]*)"', tag)
        data[name] = html.unescape(value[1]) if value else "y"
    for name, options in re.findall(
        r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', markup, re.S
    ):
        selected = next(
            (tag for tag in re.findall(r"<option[^>]*>", options) if "selected" in tag),
            re.search(r"<option[^>]*>", options)[0],
        )
        data[name] = html.unescape(re.search(r'value="([^"]*)"', selected)[1])
    return data


def memberships(db, sequence):
    with db.transaction() as session:
        return [
            (row.scene_id, row.position, row.duration_override_seconds)
            for row in d.list_sequence_memberships(session, sequence)
        ]


def test_complete_ordered_shuffle_draft_and_repeated_occurrences(
    sequences_ui, monkeypatch
):
    _, client, db, first, second, sequence, post = sequences_ui

    def forbidden(*args, **kwargs):
        pytest.fail("Configuration request started runtime or external work")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    monkeypatch.setattr("threading.Thread.start", forbidden)
    path = f"/sequences/{sequence}/edit"
    data = draft_fields(client.get(path))
    assert data["occurrences-2-scene_id"] == str(first)
    before = memberships(db, sequence)
    for action in ("up:2", "down:0", "remove:2", "add"):
        data = draft_fields(post(path, **{**data, "action": action}))
        assert memberships(db, sequence) == before
    data.update(
        {
            "name": "Changed",
            "mode": "shuffle",
            "occurrences-2-scene_id": str(second),
            "occurrences-2-duration_override_seconds": "1",
        }
    )
    assert "Sequence saved." in post(path, **data).text
    assert memberships(db, sequence) == [
        (first, 0, 86400),
        (first, 1, None),
        (second, 2, 1),
    ]
    with db.transaction() as session:
        rows = d.list_sequence_memberships(session, sequence)
        assert len({row.id for row in rows}) == 3
        assert d.get_sequence(session, sequence).mode == "shuffle"
        assert not d.get_scene(session, second).enabled
    data = draft_fields(client.get(path))
    data.update(name="New ordered", mode="ordered")
    data.pop("enabled")
    assert "Sequence saved." in post("/sequences/new", **data).text
    with db.transaction() as session:
        new = d.list_sequences(session)[-1]
        assert new.mode == "ordered" and not new.enabled
    for route in ("/sequences", path):
        text = client.get(route).text
        assert "Second disabled" in text and "Disabled" in text
        assert str(db.path) not in text
    assert (
        "Active selection saved."
        in post("/settings/active-sequence", active_sequence_id=sequence).text
    )
    assert (
        "Fallback dwell saved."
        in post("/settings/default-dwell", default_scene_dwell_seconds=45).text
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("duration_override_seconds", "0"),
        ("duration_override_seconds", "86401"),
        ("duration_override_seconds", "1.5"),
        ("duration_override_seconds", "invalid"),
        ("scene_id", "99999"),
        ("scene_id", "invalid"),
    ],
)
def test_invalid_occurrence_preserves_complete_configuration(
    sequences_ui, field, value
):
    _, client, db, _, _, sequence, post = sequences_ui
    path = f"/sequences/{sequence}/edit"
    data = draft_fields(client.get(path))
    before = memberships(db, sequence)
    data.update({"name": "Must not save", f"occurrences-1-{field}": value})
    response = post(path, **data)
    assert 'aria-invalid="true"' in response.text
    assert memberships(db, sequence) == before
    with db.transaction() as session:
        assert d.get_sequence(session, sequence).name == "Original"


def test_split_occurrences_are_visible_preserved_and_explicitly_replaced(sequences_ui):
    _, client, db, first, second, sequence, post = sequences_ui
    with db.transaction() as session:
        widget = d.create_widget(
            session,
            name="Other",
            kind="image",
            source_id=d.list_sources(session)[0].id,
            configuration={"fit": "cover"},
        )
        split = d.create_scene(
            session,
            name="Future split",
            layout="split_vertical",
            placements=[("left", widget.id), ("right", d.list_widgets(session)[0].id)],
        ).id
        d.update_sequence(
            session,
            sequence,
            name="Split list",
            mode="ordered",
            enabled=True,
            memberships=[(split, 10), (first, None), (split, 20)],
        )
    path = f"/sequences/{sequence}/edit"
    before = memberships(db, sequence)
    assert client.get("/sequences").text.count("Not executable in V0") == 2
    data = draft_fields(client.get(path))
    assert (
        data["occurrences-0-scene_id"] == data["occurrences-2-scene_id"] == str(split)
    )
    response = post(path, **{**data, "name": "Changed"})
    assert "replace or remove unsupported split occurrences" in response.text
    assert memberships(db, sequence) == before
    data = draft_fields(post(path, **{**data, "action": "up:2"}))
    assert data["occurrences-1-scene_id"] == str(split)
    assert memberships(db, sequence) == before
    data = draft_fields(post(path, **{**data, "action": "remove:0"}))
    data["occurrences-0-scene_id"] = str(second)
    assert "Sequence saved." in post(path, **data).text
    assert memberships(db, sequence) == [(second, 0, 20), (first, 1, None)]
    data["occurrences-0-scene_id"] = str(split)
    assert 'aria-invalid="true"' in post("/sequences/new", **data).text
    with db.transaction() as session:
        assert d.get_scene(session, split).layout == "split_vertical"
        assert len(d.list_sequences(session)) == 1


def test_empty_draft_rejected_and_invalid_input_survives_moves(sequences_ui):
    _, client, db, _, _, sequence, post = sequences_ui
    path = f"/sequences/{sequence}/edit"
    before = memberships(db, sequence)
    data = draft_fields(client.get(path))
    data["occurrences-0-duration_override_seconds"] = "bad value"
    data = draft_fields(post(path, **{**data, "action": "down:0"}))
    assert data["occurrences-1-duration_override_seconds"] == "bad value"
    for _ in range(3):
        data = draft_fields(post(path, **{**data, "action": "remove:0"}))
    assert "Supply at least one single Scene" in post(path, **data).text
    assert memberships(db, sequence) == before


def test_active_idle_disabled_dwell_validation_and_delete_lifecycle(sequences_ui):
    _, client, db, _, _, sequence, post = sequences_ui
    assert get_playback_settings(db).default_scene_dwell_seconds == 30
    before = memberships(db, sequence)
    path = f"/sequences/{sequence}/edit"
    data = draft_fields(client.get(path))
    data.pop("enabled")
    assert "Sequence saved." in post(path, **data).text
    assert memberships(db, sequence) == before
    with db.transaction() as session:
        assert not d.get_sequence(session, sequence).enabled
    assert "Original — Disabled" in client.get("/settings").text
    for selected in (sequence, 0, sequence):
        assert (
            "Active selection saved."
            in post("/settings/active-sequence", active_sequence_id=selected).text
        )
        assert get_playback_settings(db).active_sequence_id == (selected or None)
    for invalid in ("9999", "bad", "-1", str(2**100), ""):
        assert (
            'aria-invalid="true"'
            in post("/settings/active-sequence", active_sequence_id=invalid).text
        )
        assert get_playback_settings(db).active_sequence_id == sequence
    for dwell in (1, 86400):
        assert (
            "Fallback dwell saved."
            in post("/settings/default-dwell", default_scene_dwell_seconds=dwell).text
        )
        assert get_playback_settings(db).default_scene_dwell_seconds == dwell
    for invalid in ("0", "86401", "1.5", "bad", ""):
        assert (
            'aria-invalid="true"'
            in post("/settings/default-dwell", default_scene_dwell_seconds=invalid).text
        )
        assert get_playback_settings(db).default_scene_dwell_seconds == 86400
    response = post(f"/sequences/{sequence}/delete")
    assert "Active selection is now None / Idle" in response.text
    assert get_playback_settings(db).active_sequence_id is None
    with db.transaction() as session:
        assert session.get(d.Sequence, sequence) is None
        assert session.query(d.SequenceMembership).count() == 0
        assert len(d.list_scenes(session)) == 2
        assert len(d.list_widgets(session)) == len(d.list_sources(session)) == 1


def test_authentication_csrf_and_missing_resources(sequences_ui):
    app, client, db, _, _, sequence, _ = sequences_ui
    anonymous = app.test_client()
    token = re.search(
        r'name="csrf_token"[^>]*value="([^"]+)"', anonymous.get("/login").text
    )[1]
    pages = ["/sequences", "/sequences/new", f"/sequences/{sequence}/edit"]
    mutations = [
        *pages[1:],
        f"/sequences/{sequence}/delete",
        "/settings/active-sequence",
        "/settings/default-dwell",
    ]
    for path in pages:
        assert anonymous.get(path).location.startswith("/login")
    for path in mutations:
        assert anonymous.post(
            path, data={"csrf_token": token, "action": "add"}
        ).location.startswith("/login")
        for bad_token in (None, "bad"):
            assert (
                client.post(
                    path, data={"csrf_token": bad_token, "action": "add"}
                ).status_code
                == 400
            )
    assert client.get(f"/sequences/{sequence}/delete").status_code == 405
    assert client.get("/sequences/99999/edit").status_code == 404
    with db.transaction() as session:
        assert len(d.list_sequences(session)) == 1
