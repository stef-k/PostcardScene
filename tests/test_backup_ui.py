"""DB-only backup control plane over real authentication and CSRF."""

import re
from datetime import UTC, datetime
from importlib import import_module

import pytest

from postcardscene.backup_policy import get_backup_policy, replace_backup_policy
from postcardscene.persistence import DatabaseError

ui = import_module("postcardscene.web.backup")


def test_auth_csrf_and_literal_policy(sources_ui, monkeypatch):
    app, client, db, _, _ = sources_ui
    anonymous = app.test_client()
    assert anonymous.get("/backup").status_code == 302
    assert anonymous.post("/backup").status_code in (302, 400)
    assert client.post("/backup").status_code == 400
    token = re.search(
        r'name="csrf_token"[^>]*value="([^"]+)"', client.get("/backup").text
    )[1]
    original = ui.get_backup_status
    calls = []
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    monkeypatch.setattr(ui, "utc_now", lambda: now)

    def status(database, instant, **kwargs):
        calls.append((instant, kwargs))
        return original(database, instant, **kwargs)

    monkeypatch.setattr(ui, "get_backup_status", status)
    path = "/unavailable-nas/../literal path "
    import builtins
    import os
    from pathlib import Path

    def guard(function):
        def checked(target, *args, **kwargs):
            assert "unavailable-nas" not in str(target)
            return function(target, *args, **kwargs)

        return checked

    for name in ("stat", "lstat", "listdir", "scandir", "open", "access"):
        monkeypatch.setattr(os, name, guard(getattr(os, name)))
    monkeypatch.setattr(builtins, "open", guard(builtins.open))
    monkeypatch.setattr(Path, "resolve", guard(Path.resolve))

    def forbidden(*args, **kwargs):
        pytest.fail("Backup request attempted external work")

    for name in ("create", "verify", "list_backups"):
        monkeypatch.setattr("postcardscene.backup." + name, forbidden)
    monkeypatch.setattr("postcardscene.backup.scheduled.scheduled", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    for enabled in (True, False):
        calls.clear()
        response = client.post(
            "/backup",
            data={
                "csrf_token": token,
                "destination_path": path,
                "local_hour": "0",
                "retention_count": "30",
                **({"enabled": "y"} if enabled else {}),
            },
        )
        assert response.status_code == 302
        assert calls == [(now, {"include_destination": True})]
        policy, _ = get_backup_policy(db)
        assert (
            policy.enabled,
            policy.destination_path,
            policy.local_hour,
            policy.retention_count,
        ) == (enabled, path, 0, 30)
        calls.clear()
        page = client.get("/backup").text
        assert calls == [(now, {"include_destination": True})]
        assert 'aria-current="page">Backup' in page
        assert "not proof the archive still exists" in page


@pytest.mark.parametrize(
    "changes",
    [
        {"destination_path": ""},
        {"destination_path": "relative"},
        {"destination_path": "/" + "x" * 4096},
        {"destination_path": "/bad\0path"},
        {"local_hour": "-1"},
        {"local_hour": "24"},
        {"local_hour": "bad"},
        {"retention_count": "0"},
        {"retention_count": "31"},
    ],
)
def test_invalid_replacement_does_not_mutate(sources_ui, changes):
    _, client, db, _, _ = sources_ui
    replace_backup_policy(db, False, "/retained", 3, 7)
    before = get_backup_policy(db)
    token = re.search(
        r'name="csrf_token"[^>]*value="([^"]+)"', client.get("/backup").text
    )[1]
    response = client.post(
        "/backup",
        data={
            "csrf_token": token,
            "enabled": "y",
            "destination_path": "/valid",
            "local_hour": "3",
            "retention_count": "7",
            **changes,
        },
    )
    assert response.status_code == 200
    assert 'aria-invalid="true"' in response.text
    assert get_backup_policy(db) == before


def test_status_failure_is_bounded(sources_ui, monkeypatch):
    _, client, _, _, _ = sources_ui

    def fail(*args, **kwargs):
        raise DatabaseError("private raw exception")

    monkeypatch.setattr(ui, "get_backup_status", fail)
    response = client.get("/backup")
    assert response.status_code == 503
    assert "private raw exception" not in response.text


@pytest.mark.parametrize(
    "enabled,hour,result,success,timezone,label",
    [
        (False, 3, "never", False, "UTC", "Disabled"),
        (True, 23, "never", False, "UTC", "Not yet due"),
        (True, 3, "never", False, "UTC", "Due/overdue"),
        (True, 3, "ready", True, "UTC", "Satisfied today"),
        (True, 3, "failed", False, "UTC", "Last attempt failed"),
        (
            True,
            3,
            "ready_retention_degraded",
            True,
            "UTC",
            "Backup succeeded, retention degraded",
        ),
        (True, 3, "never", False, "Missing/Zone", "Status unavailable"),
    ],
)
def test_shared_status_rendering(
    sources_ui, monkeypatch, enabled, hour, result, success, timezone, label
):
    from sqlalchemy import text

    _, client, db, _, _ = sources_ui
    replace_backup_policy(db, enabled, "/private-destination", hour, 7)
    name = "postcardscene-backup-20260910T030000000000Z-v0.1.0.dev0.tar.gz"
    with db.transaction(write=True) as session:
        session.execute(
            text("UPDATE application_settings SET timezone=:zone"), {"zone": timezone}
        )
        session.execute(
            text(
                "UPDATE backup_policy SET last_result=:result, last_attempt_ns=:attempt, last_success_ns=:success, last_success_local_date=:day, last_archive_filename=:name"
            ),
            {
                "result": result,
                "attempt": 1 if result != "never" else None,
                "success": 1 if success else None,
                "day": "2026-09-10" if success else None,
                "name": name if success else None,
            },
        )
    monkeypatch.setattr(ui, "utc_now", lambda: datetime(2026, 9, 10, 12, tzinfo=UTC))
    page = client.get("/backup").text
    assert label in page
    assert (name in page) == success
    if not success:
        assert "No successful scheduled backup recorded yet" in page
