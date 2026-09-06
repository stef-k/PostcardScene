import re
import sqlite3
from importlib.metadata import version

import pytest
from alembic import command
from sqlalchemy.exc import IntegrityError

from postcardscene.accounts import set_password
from postcardscene.persistence import SCHEMA_REVISION, Database, DatabaseError
from postcardscene.schema import migration_config, upgrade_database
from postcardscene.settings import ApplicationSettings, get_timezone, set_timezone
from postcardscene.web import create_app


def csrf(client, path="/settings"):
    return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', client.get(path).text)[1]


@pytest.fixture
def app(tmp_path):
    app = create_app({"TESTING": True, "DATABASE_PATH": tmp_path / "state.sqlite3"})
    database = app.extensions["postcardscene.database"]
    upgrade_database(database.path)
    set_password(database, "admin", "settings test password", initial=True)
    yield app
    database.engine.dispose()


@pytest.fixture
def client(app):
    client = app.test_client()
    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "settings test password",
            "csrf_token": csrf(client, "/login"),
        },
    )
    assert response.status_code == 302
    return client


def test_explicit_settings_migration_and_preservation(tmp_path):
    database = Database(tmp_path / "old.sqlite3", create=True)
    with database.engine.begin() as connection:
        command.upgrade(migration_config(connection), "0002_administrator")
        connection.exec_driver_sql(
            "INSERT INTO administrator VALUES (1, 'admin', 'existing-hash', 'identity')"
        )
    create_app({"DATABASE_PATH": database.path})
    with pytest.raises(DatabaseError):
        get_timezone(database)
    assert upgrade_database(database.path).schema_revision == SCHEMA_REVISION
    assert get_timezone(database) == "UTC"
    set_timezone(database, "Europe/Athens")
    upgrade_database(database.path)
    assert get_timezone(database) == "Europe/Athens"
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT * FROM administrator").one() == (
            1,
            "admin",
            "existing-hash",
            "identity",
        )
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM application_settings"
            ).scalar_one()
            == 1
        )
    with pytest.raises(IntegrityError):
        with database.transaction() as session:
            session.add(ApplicationSettings(id=2, timezone="UTC"))
    database.engine.dispose()


def test_pages_and_writes_require_authentication(app):
    anonymous = app.test_client()
    for path in ["/", "/settings"]:
        assert anonymous.get(path).location.startswith("/login")
    response = anonymous.post(
        "/settings",
        data={"timezone": "Europe/Athens", "csrf_token": csrf(anonymous, "/login")},
    )
    assert response.location.startswith("/login")
    assert get_timezone(app.extensions["postcardscene.database"]) == "UTC"


def test_timezone_persists_across_reconstruction_and_requires_csrf(app, client):
    assert 'value="UTC"' in client.get("/settings").text
    for token in [None, "invalid"]:
        data = {"timezone": "Europe/Athens"}
        if token is not None:
            data["csrf_token"] = token
        assert client.post("/settings", data=data).status_code == 400
    assert get_timezone(app.extensions["postcardscene.database"]) == "UTC"
    response = client.post(
        "/settings",
        data={"timezone": "Europe/Athens", "csrf_token": csrf(client)},
        follow_redirects=True,
    )
    assert "Settings saved." in response.text
    rebuilt = create_app(app.config)
    fresh = rebuilt.test_client()
    fresh.set_cookie(
        "postcardscene_session", client.get_cookie("postcardscene_session").value
    )
    assert 'value="Europe/Athens"' in fresh.get("/settings").text
    assert get_timezone(rebuilt.extensions["postcardscene.database"]) == "Europe/Athens"


@pytest.mark.parametrize("timezone", ["Mars/Olympus", "../UTC", "/etc/passwd", ""])
def test_invalid_timezone_leaves_previous_value(app, client, timezone):
    database = app.extensions["postcardscene.database"]
    set_timezone(database, "Europe/Athens")
    response = client.post(
        "/settings", data={"timezone": timezone, "csrf_token": csrf(client)}
    )
    assert response.status_code == 200
    assert 'aria-invalid="true"' in response.text
    assert "Enter an IANA timezone" in response.text
    assert get_timezone(database) == "Europe/Athens"


def test_dashboard_real_database_health_and_unavailable_runtime(app, client):
    response = client.get("/")
    assert response.status_code == 200
    for text in [
        "PostcardScene",
        version("postcardscene"),
        SCHEMA_REVISION,
        "Healthy",
        "Not yet connected",
    ]:
        assert text in response.text
    database = app.extensions["postcardscene.database"]
    # Real integrity failure that leaves the administrator/session table usable.
    with sqlite3.connect(database.path) as connection:
        connection.executescript(
            "CREATE TABLE broken (id INTEGER REFERENCES administrator(id));"
            "INSERT INTO broken VALUES (999);"
        )
    response = client.get("/")
    assert response.status_code == 200
    assert "Needs attention" in response.text
    assert "Not yet connected" in response.text
    assert "Healthy" not in response.text
    for private in [
        str(database.path),
        "existing-hash",
        "PRAGMA",
        "foreign-key",
        "session_id",
    ]:
        assert private not in response.text
