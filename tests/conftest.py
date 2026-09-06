import pytest

from postcardscene.session_secret import initialize_secret


@pytest.fixture(autouse=True)
def installation_secret(tmp_path, monkeypatch):
    path = tmp_path / "session.key"
    initialize_secret(path)
    monkeypatch.setattr("postcardscene.web.DEFAULT_SECRET_PATH", path)
    return path


@pytest.fixture
def catalog(tmp_path):
    from postcardscene import domain as d
    from postcardscene.filesystem_source import PathPolicy
    from postcardscene.persistence import Database
    from postcardscene.schema import upgrade_database

    root = tmp_path / "photos"
    root.mkdir()
    db = Database(tmp_path / "catalog.sqlite3")
    upgrade_database(db.path)
    with db.transaction() as session:
        source_id = d.create_source(
            session,
            name="Photos",
            kind="local_directory",
            configuration={"path": str(root), "recursive": True},
        ).id
    yield db, source_id, root, PathPolicy([root])
    db.engine.dispose()


@pytest.fixture
def sources_ui(catalog):
    import re

    from postcardscene.accounts import set_password
    from postcardscene.web import create_app

    db, source_id, root, _ = catalog
    app = create_app(
        {"TESTING": True, "DATABASE_PATH": db.path, "MEDIA_ALLOWED_ROOTS": [str(root)]}
    )
    set_password(db, "admin", "sources test password", initial=True)
    client = app.test_client()
    token = re.search(
        r'name="csrf_token"[^>]*value="([^"]+)"', client.get("/login").text
    )[1]
    client.post(
        "/login",
        data={
            "username": "admin",
            "password": "sources test password",
            "csrf_token": token,
        },
    )
    yield app, client, db, source_id, root
    app.extensions["postcardscene.database"].engine.dispose()


@pytest.fixture
def source_post(sources_ui):
    import re

    _, client, _, _, root = sources_ui
    token = re.search(
        r'name="csrf_token"[^>]*value="([^"]+)"', client.get("/sources/new").text
    )[1]

    def post(route="/sources/new", **changes):
        data = dict(
            name="Library",
            kind="local_directory",
            path=str(root),
            recursive="y",
            enabled="y",
            csrf_token=token,
        )
        data.update(changes)
        return client.post(
            route,
            data={key: value for key, value in data.items() if value is not None},
            follow_redirects=True,
        )

    return post
