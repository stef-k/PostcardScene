import json
import re
import sqlite3
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from postcardscene.accounts import Administrator, set_password
from postcardscene.configuration import load_runtime_config
from postcardscene.doctor import Report, bounded
from postcardscene.domain import SOURCE_KINDS, WIDGET_KINDS
from postcardscene.filesystem_source import PathPolicy, validate_source
from postcardscene.image_selection import validate_image_configuration
from postcardscene.panel_client import _decode
from postcardscene.persistence import Base, Database, DatabaseError
from postcardscene.schema import migration_config, upgrade_database
from postcardscene.session_secret import initialize_secret, read_secret
from postcardscene.status import database_status
from postcardscene.video_selection import validate_video_configuration
from postcardscene.web import create_app
from postcardscene.web_selection import validate_web_configuration, validate_web_source

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
    app = create_app(
        {"TESTING": True, "SESSION_SECRET_PATH": path, "SECRET_KEY": "configured"}
    )
    assert app.test_client().get("/login").status_code == 503
    assert not path.exists()
    runner = app.test_cli_runner()
    assert runner.invoke(args=["auth", "init-secret"]).exit_code == 0
    before = read_secret(path)
    assert runner.invoke(args=["auth", "init-secret"]).exit_code == 0
    assert read_secret(path) == before
    assert (
        create_app({"SESSION_SECRET_PATH": path, "SECRET_KEY": "configured"}).secret_key
        == before
    )
    path.chmod(0o644)
    with pytest.raises(ValueError):
        create_app({"SESSION_SECRET_PATH": path})
    assert path.read_bytes() == before
    path.chmod(0o600)
    link = tmp_path / "link.key"
    link.symlink_to(path)
    with pytest.raises(OSError):
        initialize_secret(link)
    path.write_bytes(b"invalid")
    with pytest.raises(ValueError):
        create_app({"SESSION_SECRET_PATH": path})
    assert path.read_bytes() == b"invalid"


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


def test_configured_fallback_cannot_authorize_a_browser_session(app):
    client = app.test_client()
    assert login(client).status_code == 302
    cookie = client.get_cookie("postcardscene_session").value
    serializer = app.session_interface.get_signing_serializer(app)
    session_data = serializer.loads(cookie)
    rebuilt = create_app({**app.config, "SECRET_KEY_FALLBACKS": ["operator-key"]})
    assert rebuilt.config["SECRET_KEY_FALLBACKS"] is None
    # Construct a correctly shaped cookie signed only by the configured fallback.
    app.secret_key = "operator-key"
    forged = app.session_interface.get_signing_serializer(app).dumps(session_data)
    browser = rebuilt.test_client()
    browser.set_cookie("postcardscene_session", forged)
    assert browser.get("/").status_code == 302
    browser.set_cookie("postcardscene_session", cookie)
    assert browser.get("/").status_code == 200


def durable_rows(database):
    with sqlite3.connect(database.path) as connection:
        return tuple(
            line for line in connection.iterdump() if '"administrator"' not in line
        )


def test_reset_preserves_all_unrelated_durable_state(app, caplog):
    from postcardscene.domain import create_source

    database = app.extensions["postcardscene.database"]
    with database.transaction() as transaction:
        create_source(
            transaction,
            name="Retained",
            kind="web_url",
            configuration={"url": "https://example.invalid"},
        )
        admin = transaction.get(Administrator, 1)
        previous = (admin.password_hash, admin.session_id)
    before = durable_rows(database)
    key_path = app.config["SESSION_SECRET_PATH"]
    key = key_path.read_bytes()
    result = app.test_cli_runner().invoke(
        args=["auth", "reset-password", "--username", "admin"],
        input="replacement audit password\nreplacement audit password\n",
    )
    assert result.exit_code == 0
    assert durable_rows(database) == before
    assert key_path.read_bytes() == key
    with database.transaction() as transaction:
        admin = transaction.get(Administrator, 1)
        assert admin.username == "admin"
        assert admin.password_hash.startswith("scrypt:")
        assert admin.password_hash != previous[0] and admin.session_id != previous[1]
    for value in (*previous, PASSWORD, "replacement audit password", key.hex()):
        assert value not in result.output + caplog.text


def test_known_diagnostics_and_errors_do_not_disclose_auth_values(
    app, monkeypatch, caplog
):
    caplog.set_level("INFO")
    client = app.test_client()
    assert login(client).status_code == 302
    csrf = token(client, "/")
    cookie = client.get_cookie("postcardscene_session").value
    database = app.extensions["postcardscene.database"]
    with database.transaction() as transaction:
        admin = transaction.get(Administrator, 1)
        values = (
            PASSWORD,
            admin.password_hash,
            admin.session_id,
            app.secret_key.hex(),
            csrf,
            cookie,
        )
    injected = " ".join(values)

    def fail(*args, **kwargs):
        raise DatabaseError(injected)

    status = database_status(SimpleNamespace(check=fail))
    report = Report((bounded.inspect_bounded(fail, "database"),))
    rendered = (
        json.dumps(asdict(status)) + report.render() + report.render(structured=True)
    )
    # CSRF errors render a fixed error page, never the rejected token or form body.
    response = app.test_client().post("/logout", data={"csrf_token": injected})
    assert response.status_code == 400
    rendered += response.text

    def failed_write(*args, **kwargs):
        raise SQLAlchemyError(injected)

    monkeypatch.setattr("postcardscene.web.auth_cli.set_password", failed_write)
    result = app.test_cli_runner().invoke(
        args=["auth", "reset-password", "--username", "admin"],
        input=f"{PASSWORD}\n{PASSWORD}\n",
    )
    assert result.exit_code != 0
    rendered += result.output + caplog.text
    with pytest.raises(ValueError) as error:
        _decode(json.dumps({"version": 1, "outcome": injected, "status": {}}))
    rendered += str(error.value)
    assert app.secret_key not in rendered.encode()
    assert all(value not in rendered for value in values)


@pytest.mark.parametrize(
    "field", ["password", "api_key", "bearer_token", "client_secret", "credentials"]
)
def test_v0_semantic_configuration_has_no_provider_credential_fields(tmp_path, field):
    assert field.upper() not in create_app().config
    assert field.upper() not in load_runtime_config()
    secret = {field: "test-only provider value"}
    validators = (
        lambda: validate_source(
            "local_directory",
            {"path": str(tmp_path), "recursive": True, **secret},
            PathPolicy([tmp_path]),
        ),
        lambda: validate_web_source(
            "web_url", {"url": "https://example.invalid", **secret}
        ),
        lambda: validate_image_configuration("image", secret),
        lambda: validate_image_configuration("portrait_image_pair", secret),
        lambda: validate_video_configuration("video", secret),
        lambda: validate_web_configuration("web_view", secret),
    )
    for validate in validators:
        with pytest.raises(ValueError):
            validate()
    assert SOURCE_KINDS == {"local_directory", "mounted_directory", "web_url"}
    assert WIDGET_KINDS == {"image", "portrait_image_pair", "video", "web_view"}
    assert set(Administrator.__table__.columns.keys()) == {
        "id",
        "username",
        "password_hash",
        "session_id",
    }
    assert not any(field in table.columns for table in Base.metadata.tables.values())


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_recovery_requires_existing_signing_authority(app, damage):
    database = app.extensions["postcardscene.database"]
    with sqlite3.connect(database.path) as connection:
        before = tuple(connection.iterdump())
    path = app.config["SESSION_SECRET_PATH"]
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupt authority")
    result = app.test_cli_runner().invoke(
        args=["auth", "reset-password", "--username", "admin"],
        input=f"{PASSWORD}\n{PASSWORD}\n",
    )
    assert result.exit_code != 0
    assert PASSWORD not in result.output
    with sqlite3.connect(database.path) as connection:
        assert tuple(connection.iterdump()) == before
    if damage == "missing":
        assert not path.exists()
    else:
        assert path.read_bytes() == b"corrupt authority"
