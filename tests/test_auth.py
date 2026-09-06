import re

import pytest
from alembic import command
from sqlalchemy import select

from postcardscene.accounts import Administrator, set_password
from postcardscene.persistence import Database, DatabaseError
from postcardscene.schema import migration_config, upgrade_database
from postcardscene.session_secret import initialize_secret, read_secret
from postcardscene.web import create_app

PASSWORD = "test-only long password"


@pytest.fixture
def app(tmp_path):
    app = create_app({"TESTING": True, "DATABASE_PATH": tmp_path / "state.sqlite3"})
    runner = app.test_cli_runner()
    assert runner.invoke(args=["db", "upgrade"]).exit_code == 0
    result = runner.invoke(
        args=["auth", "create-admin", "--username", "admin"],
        input=f"{PASSWORD}\n{PASSWORD}\n",
    )
    assert result.exit_code == 0, result.output
    assert PASSWORD not in result.output
    return app


def token(client, path="/login"):
    response = client.get(path)
    return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', response.text)[1]


def login(client, password=PASSWORD, username="admin"):
    return client.post(
        "/login?next=https://untrusted.example",
        data={"username": username, "password": password, "csrf_token": token(client)},
    )


def test_access_login_csrf_and_logout_revocation(app):
    client = app.test_client()
    assert client.get("/").status_code == 302
    assert client.get("/login").status_code == 200
    assert (
        client.post(
            "/login", data={"username": "admin", "password": PASSWORD}
        ).status_code
        == 400
    )
    assert login(client, "incorrect").status_code == 401
    assert login(client, username="absent").status_code == 401
    response = login(client)
    assert response.location == "/"
    cookie = client.get_cookie("postcardscene_session")
    assert cookie.http_only and cookie.same_site == "Lax" and not cookie.secure
    assert "no-store" in response.headers["Cache-Control"]
    assert client.get("/").status_code == 200
    assert client.get("/logout").status_code == 405
    assert client.post("/logout").status_code == 400
    csrf = token(client, "/")
    assert client.post("/logout", data={"csrf_token": csrf}).status_code == 302
    assert client.get("/").status_code == 302
    client.set_cookie("postcardscene_session", cookie.value)
    assert client.get("/").status_code == 302


def test_restart_reset_hashing_and_bootstrap_refusal(app):
    client = app.test_client()
    login(client)
    cookie = client.get_cookie("postcardscene_session")
    rebuilt = create_app(app.config).test_client()
    rebuilt.set_cookie("postcardscene_session", cookie.value)
    assert rebuilt.get("/").status_code == 200
    database = app.extensions["postcardscene.database"]
    with database.transaction() as session:
        stored = session.scalar(select(Administrator.password_hash))
    assert stored.startswith("scrypt:") and PASSWORD not in stored
    runner = app.test_cli_runner()
    args = ["auth", "create-admin", "--username", "admin"]
    assert runner.invoke(args=args, input=f"{PASSWORD}\n{PASSWORD}\n").exit_code != 0
    replacement = "replacement test password"
    result = runner.invoke(
        args=["auth", "reset-password", "--username", "admin"],
        input=f"{replacement}\n{replacement}\n",
    )
    assert result.exit_code == 0, result.output
    assert replacement not in result.output and stored not in result.output
    assert rebuilt.get("/").status_code == 302
    assert login(rebuilt).status_code == 401
    assert login(rebuilt, replacement).status_code == 302
    with pytest.raises(ValueError):
        set_password(database, "absent", replacement)
    with pytest.raises(ValueError):
        set_password(database, "admin", "short")


def test_secret_authority_and_missing_setup(tmp_path):
    path = tmp_path / "new.key"
    app = create_app({"TESTING": True, "SESSION_SECRET_PATH": path})
    assert app.test_client().get("/login").status_code == 503
    assert not path.exists()
    runner = app.test_cli_runner()
    assert runner.invoke(args=["auth", "init-secret"]).exit_code == 0
    before = read_secret(path)
    assert runner.invoke(args=["auth", "init-secret"]).exit_code == 0
    assert read_secret(path) == before
    assert create_app({"SESSION_SECRET_PATH": path}).secret_key == before
    path.chmod(0o644)
    with pytest.raises(ValueError):
        create_app({"SESSION_SECRET_PATH": path})
    path.chmod(0o600)
    link = tmp_path / "link.key"
    link.symlink_to(path)
    with pytest.raises(OSError):
        initialize_secret(link)
    path.write_bytes(b"invalid")
    with pytest.raises(ValueError):
        create_app({"SESSION_SECRET_PATH": path})


def test_explicit_upgrade_from_baseline(tmp_path):
    database = Database(tmp_path / "old.sqlite3", create=True)
    with database.engine.begin() as connection:
        command.upgrade(migration_config(connection), "0001_baseline")
    with pytest.raises(DatabaseError):
        with database.transaction():
            pass
    upgrade_database(database.path)
    set_password(database, "admin", PASSWORD, initial=True)
    upgrade_database(database.path)
    with database.transaction() as session:
        assert session.get(Administrator, 1).username == "admin"


def test_https_cookie_configuration(app):
    app.config["SESSION_COOKIE_SECURE"] = True
    client = app.test_client()
    response = client.get("/login", base_url="https://localhost")
    assert "; Secure;" in response.headers["Set-Cookie"]
