"""Authenticated schedule configuration over real SQLite, sessions and CSRF."""

import re
from datetime import UTC, datetime
from importlib import import_module

import pytest

from postcardscene.operating_schedule import get_operating_schedule
from postcardscene.settings import set_timezone


@pytest.fixture
def schedule_ui(sources_ui):
    app, client, db, _, _ = sources_ui
    token = re.search(
        r'name="csrf_token"[^>]*value="([^"]+)"', client.get("/settings").text
    )[1]

    def post(path="/schedule", **data):
        return client.post(
            path, data={"csrf_token": token, **data}, follow_redirects=True
        )

    return app, client, db, post


def windows(*rows, enabled=True):
    data = {"enabled": "y"} if enabled else {}
    for index, (day, start, end) in enumerate(rows):
        data.update(
            {
                f"windows-{index}-weekday": str(day),
                f"windows-{index}-start": start,
                f"windows-{index}-end": end,
            }
        )
    return data


def test_complete_save_drafts_and_no_runtime_work(schedule_ui, monkeypatch):
    _, client, db, post = schedule_ui

    def forbidden(*args, **kwargs):
        pytest.fail("Schedule request started external or runtime work")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    monkeypatch.setattr("threading.Thread.start", forbidden)
    assert "Active — schedule disabled" in client.get("/schedule").text
    assert "Schedule saved." in post().text
    assert not get_operating_schedule(db).enabled
    assert "Sleep — weekly schedule" in post(enabled="y").text
    data = windows(
        *[(day, "00:00", "00:00") for day in range(7)],
        (0, "09:00", "12:00"),
        (0, "11:00", "13:00"),
    )
    assert "Schedule saved." in post(**data).text
    before = get_operating_schedule(db)
    assert len(before.windows) == 9
    assert {w.weekday for w in before.windows} == set(range(7))
    assert before.windows[0].end_minute == 1440
    for action in ("add", "remove:0"):
        response = post(**{**data, "windows-1-start": "invalid", "action": action})
        assert 'value="invalid"' in response.text
        assert get_operating_schedule(db) == before
    assert "Schedule saved." in post(**windows(enabled=False)).text
    assert get_operating_schedule(db).windows == ()
    assert not get_operating_schedule(db).enabled


@pytest.mark.parametrize(
    "day,start,end",
    [
        (7, "09:00", "10:00"),
        ("bad", "09:00", "10:00"),
        (0, "24:00", "00:00"),
        (0, "9:00", "10:00"),
        (0, "09:60", "10:00"),
        (0, "", "10:00"),
        (0, "09:00", "24:00"),
        (0, "23:00", "01:00"),
        (0, "09:00", "09:00"),
    ],
)
def test_invalid_row_preserves_complete_schedule(schedule_ui, day, start, end):
    _, _, db, post = schedule_ui
    post(**windows((1, "08:00", "17:00")))
    before = get_operating_schedule(db)
    response = post(**windows((2, "01:00", "02:00"), (day, start, end), enabled=False))
    assert 'aria-invalid="true"' in response.text
    assert get_operating_schedule(db) == before
    if start in ("23:00", "09:00") and end in ("01:00", "09:00"):
        assert "two windows on adjacent days" in response.text


def test_window_limit_and_draft_removal(schedule_ui):
    _, _, db, post = schedule_ui
    data = windows(*[(0, "00:00", "00:00")] * 64)
    assert "Schedule saved." in post(**data).text
    before = get_operating_schedule(db)
    assert "at most 64" in post(**data, action="add").text
    too_many = windows(*[(0, "00:00", "00:00")] * 65)
    assert "at most 64" in post(**too_many).text
    assert get_operating_schedule(db) == before
    response = post(**windows((0, "08:00", "09:00")), action="remove:0")
    assert 'name="windows-0-start"' not in response.text
    assert get_operating_schedule(db) == before
    assert post(action="remove:999").status_code == 400


def test_timezone_decision_override_expiry_and_clear(schedule_ui, monkeypatch):
    _, client, db, post = schedule_ui
    now = datetime(2026, 9, 7, 21, 30, tzinfo=UTC)
    monkeypatch.setattr(
        import_module("postcardscene.web.schedule"), "utc_now", lambda: now
    )
    set_timezone(db, "Europe/Athens")
    response = post(**windows((1, "00:00", "01:00")))
    assert "Active — weekly schedule" in response.text
    assert "Europe/Athens" in response.text and 'href="/settings"' in response.text
    before = get_operating_schedule(db).windows
    for state, label, duration in (("sleep", "Sleep", 1), ("active", "Active", 10080)):
        response = post("/schedule/override", state=state, duration_minutes=duration)
        saved = get_operating_schedule(db)
        assert saved.override_active is (state == "active")
        assert saved.override_until_utc == int(now.timestamp()) + duration * 60
        assert saved.windows == before and saved.enabled
        assert f"{label} — temporary override" in response.text
        assert "2026-09-" in response.text and "+0300" in response.text
    for duration in ("0", "10081", "1.5", "bad", ""):
        response = post("/schedule/override", state="sleep", duration_minutes=duration)
        assert 'aria-invalid="true"' in response.text
        assert get_operating_schedule(db) == saved
    assert (
        'aria-invalid="true"'
        in post("/schedule/override", state="bad", duration_minutes=1).text
    )
    assert get_operating_schedule(db) == saved
    now = datetime(2026, 9, 14, 21, 30, tzinfo=UTC)
    response = client.get("/schedule")
    assert "Expired" in response.text and "Active — weekly schedule" in response.text
    assert "Active — temporary override" not in response.text
    response = post("/schedule/resume")
    assert "Temporary override cleared." in response.text
    assert get_operating_schedule(db).override_active is None
    assert get_operating_schedule(db).windows == before
    post(**windows((1, "00:00", "01:00"), enabled=False))
    assert (
        "Sleep — temporary override"
        in post("/schedule/override", state="sleep", duration_minutes=5).text
    )
    assert not get_operating_schedule(db).enabled
    assert get_operating_schedule(db).windows == before
    assert "Active — schedule disabled" in post("/schedule/resume").text


def test_auth_csrf_and_sanitized_database_failure(schedule_ui, monkeypatch):
    app, client, db, post = schedule_ui
    anonymous = app.test_client()
    token = re.search(
        r'name="csrf_token"[^>]*value="([^"]+)"', anonymous.get("/login").text
    )[1]
    assert anonymous.get("/schedule").location.startswith("/login")
    before = get_operating_schedule(db)
    for path, data in (
        ("/schedule", {}),
        ("/schedule", {"action": "add"}),
        ("/schedule", {"action": "remove:0"}),
        ("/schedule/override", {"state": "sleep", "duration_minutes": 1}),
        ("/schedule/resume", {}),
    ):
        assert anonymous.post(
            path, data={"csrf_token": token, **data}
        ).location.startswith("/login")
        for invalid in (None, "bad"):
            assert (
                client.post(path, data={"csrf_token": invalid, **data}).status_code
                == 400
            )
    assert get_operating_schedule(db) == before
    for path in ("/schedule/override", "/schedule/resume"):
        assert client.get(path).status_code == 405

    from postcardscene.persistence import DatabaseError

    def unavailable(*args, **kwargs):
        raise DatabaseError(f"private failure {db.path}")

    monkeypatch.setattr(
        import_module("postcardscene.web.schedule"),
        "get_operating_schedule",
        unavailable,
    )
    response = client.get("/schedule")
    assert response.status_code == 503
    assert "Configuration could not be loaded or saved" in response.text
    assert str(db.path) not in response.text and "private failure" not in response.text
