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
