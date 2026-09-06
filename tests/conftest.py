import pytest

from postcardscene.session_secret import initialize_secret


@pytest.fixture(autouse=True)
def installation_secret(tmp_path, monkeypatch):
    path = tmp_path / "session.key"
    initialize_secret(path)
    monkeypatch.setattr("postcardscene.web.DEFAULT_SECRET_PATH", path)
    return path
