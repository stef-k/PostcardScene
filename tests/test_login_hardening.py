"""Login policy at the HTTP seam and bounded concurrent admission."""

import re
from concurrent.futures import ThreadPoolExecutor

import pytest
from waitress.server import create_server
from werkzeug.test import Client

from postcardscene.accounts import set_password
from postcardscene.schema import upgrade_database
from postcardscene.web import create_app
from postcardscene.web.login_limiter import LoginLimiter
from postcardscene.web.server import production_app


@pytest.fixture
def login_app(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("postcardscene.web.login_limiter.monotonic", lambda: now[0])
    app = create_app({"TESTING": True, "DATABASE_PATH": tmp_path / "db"})
    upgrade_database(tmp_path / "db")
    set_password(
        app.extensions["postcardscene.database"], "admin", "test password", initial=True
    )
    return app, now


def submit(client, username="absent", password="private-password", **kwargs):
    csrf = re.search(
        r'name="csrf_token"[^>]*value="([^"]+)"', client.get("/login", **kwargs).text
    )[1]
    return client.post(
        "/login",
        data=dict(username=username, password=password, csrf_token=csrf),
        **kwargs,
    )


def test_window_privacy_isolation_and_restart(login_app, caplog):
    app, now = login_app
    client = app.test_client()
    for index in range(5):
        response = submit(client, username="admin" if index % 2 else "absent")
        assert response.status_code == 401
        assert "Unable to log in." in response.text
        assert 'value="admin"' not in response.text
        assert "absent" not in response.text and "private-password" not in response.text
        now[0] += 10
    response = submit(client, "admin", "test password")
    assert response.status_code == 429 and response.headers["Retry-After"] == "250"
    assert "Unable to log in." in response.text
    assert client.post("/login").status_code == 429
    assert (
        submit(client, environ_overrides={"REMOTE_ADDR": "192.0.2.2"}).status_code
        == 401
    )
    now[0] = 1299.2
    assert submit(client).headers["Retry-After"] == "1"
    now[0] = 1300
    assert submit(client).status_code == 401
    assert submit(client).status_code == 429
    assert submit(create_app(app.config).test_client()).status_code == 401
    assert "private-password" not in caplog.text and "absent" not in caplog.text


def test_success_and_malformed_requests(login_app):
    app, _ = login_app
    client = app.test_client()
    assert (
        client.post(
            "/login", data={"username": "admin", "password": "test password"}
        ).status_code
        == 400
    )
    for _ in range(4):
        assert submit(client).status_code == 401
    assert submit(client, "admin", "test password").status_code == 302
    other_session = app.test_client()
    for _ in range(5):
        assert submit(other_session, username="").status_code == 401
    assert submit(other_session).status_code == 429


def test_bound_eviction_and_concurrent_admission():
    limiter = LoginLimiter()
    for index in range(256):
        entry, retry = limiter.begin(str(index))
        assert retry == 0
        limiter.finish(str(index), entry, failed=True)
    entry, _ = limiter.begin("0")
    limiter.finish("0", entry, failed=True)
    entry, _ = limiter.begin("new")
    limiter.finish("new", entry, failed=True)
    assert len(limiter._entries) == 256
    assert "0" in limiter._entries and "1" not in limiter._entries
    limiter = LoginLimiter()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(limiter.begin, ["same"] * 20))
    admitted = [entry for entry, retry in results if not retry]
    assert len(admitted) == 5
    for entry in admitted:
        limiter.finish("same", entry, failed=True)
    assert 1 <= limiter.retry_after("same") <= 300


@pytest.mark.parametrize("mode", ["direct_http", "reverse_proxy_https"])
def test_waitress_client_ip_is_only_limiter_authority(login_app, mode):
    source, _ = login_app
    app, options = production_app(
        {**source.config, "TRUSTED_HOSTS": ["localhost"], "WEB_TRANSPORT_MODE": mode}
    )
    listener = create_server(app, **{**options, "port": 0})
    try:
        client = Client(listener.application)
        for index in range(6):
            headers = {
                "X-Forwarded-For": f"198.51.100.{index}, 192.0.2.1",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "localhost",
                "Referer": "https://localhost/login",
            }
            response = submit(
                client, headers=headers, environ_overrides={"REMOTE_ADDR": "127.0.0.1"}
            )
            assert response.status_code == (401 if index < 5 else 429)
        keys = list(app.extensions["postcardscene.login_limiter"]._entries)
        assert keys == (
            ["192.0.2.1"] if mode == "reverse_proxy_https" else ["127.0.0.1"]
        )
    finally:
        listener.task_dispatcher.shutdown()
        listener.close()


def test_unknown_user_still_verifies_hash(login_app, monkeypatch):
    from postcardscene.accounts import authenticate

    app, _ = login_app
    checked = []
    monkeypatch.setattr(
        "postcardscene.accounts.check_password_hash",
        lambda stored, password: checked.append(stored) or True,
    )
    database = app.extensions["postcardscene.database"]
    assert authenticate(database, "absent", "test password") is None
    assert authenticate(database, "admin", "test password")
    assert len(checked) == 2 and checked[0] == checked[1]
