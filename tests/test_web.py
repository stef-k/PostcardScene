import pytest

from postcardscene.web import create_app


def test_factory_configuration_is_explicit_and_isolated(tmp_path, monkeypatch):
    config = tmp_path / "control.py"
    config.write_text("TRUSTED_HOSTS = ['control.example']\n")
    monkeypatch.setenv("POSTCARDSCENE_CONFIG", str(config))
    app = create_app({"TESTING": True})
    assert app.config["TRUSTED_HOSTS"] == ["control.example"]
    assert (
        app.test_client().get("/", base_url="http://control.example").status_code == 200
    )
    assert (
        app.test_client().get("/", base_url="http://untrusted.example").status_code
        == 400
    )
    monkeypatch.delenv("POSTCARDSCENE_CONFIG")
    fresh = create_app()
    assert not fresh.debug
    assert not fresh.testing
    assert fresh.secret_key is None
    assert fresh.config["TRUSTED_HOSTS"] is None


def test_missing_explicit_configuration_fails_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("POSTCARDSCENE_CONFIG", str(tmp_path / "missing.py"))
    with pytest.raises(OSError):
        create_app()


def test_home_and_packaged_stylesheet():
    client = create_app({"TESTING": True}).test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert b"PostcardScene" in response.data
    assert b'<nav aria-label="Main navigation">' in response.data
    assert b'name="viewport"' in response.data
    assert client.get("/static/control.css").status_code == 200


def test_errors_hide_details_and_preserve_http_semantics():
    app = create_app()

    @app.get("/failure")
    def failure():
        raise RuntimeError("private-error-sentinel")

    client = app.test_client()
    for path, status in [("/missing", 404), ("/failure", 500)]:
        response = client.get(path)
        assert response.status_code == status
        assert b"PostcardScene" in response.data
        assert b"private-error-sentinel" not in response.data
        assert b"Traceback" not in response.data
    response = client.post("/")
    assert response.status_code == 405
    assert "GET" in response.headers["Allow"]
