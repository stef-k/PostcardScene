from html.parser import HTMLParser

import pytest

from postcardscene.web import create_app


def test_factory_configuration_is_explicit_and_isolated(tmp_path, monkeypatch):
    config = tmp_path / "control.py"
    config.write_text("TRUSTED_HOSTS = ['control.example']\n")
    monkeypatch.setenv("POSTCARDSCENE_CONFIG", str(config))
    app = create_app({"TESTING": True})
    assert app.config["TRUSTED_HOSTS"] == ["control.example"]
    assert (
        app.test_client().get("/", base_url="http://control.example").status_code == 302
    )
    assert (
        app.test_client().get("/", base_url="http://untrusted.example").status_code
        == 400
    )
    monkeypatch.delenv("POSTCARDSCENE_CONFIG")
    fresh = create_app()
    assert not fresh.debug
    assert not fresh.testing
    assert len(fresh.secret_key) == 32
    assert fresh.config["TRUSTED_HOSTS"] is None


def test_missing_explicit_configuration_fails_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("POSTCARDSCENE_CONFIG", str(tmp_path / "missing.py"))
    with pytest.raises(OSError):
        create_app()


class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self.assets.append(attrs["src"])
        elif tag == "link" and attrs.get("rel") == "stylesheet":
            self.assets.append(attrs["href"])


def test_home_and_packaged_assets():
    client = create_app({"TESTING": True}).test_client()
    response = client.get("/login")
    assert response.status_code == 200
    assert b"PostcardScene" in response.data
    assert b'aria-label="Main navigation"' in response.data
    assert b'name="viewport"' in response.data
    assert b'data-bs-theme="dark"' in response.data
    assert b'type="module"' in response.data
    assert b'aria-pressed="false"' in response.data
    parser = AssetParser()
    parser.feed(response.text)
    assert parser.assets == [
        "/static/vendor/bootstrap-5.3.8/bootstrap.min.css",
        "/static/control.css",
        "/static/control.js",
        "/static/vendor/bootstrap-5.3.8/bootstrap.bundle.min.js",
    ]
    for path in parser.assets:
        asset = client.get(path)
        assert asset.status_code == 200
        assert asset.data
        assert "text/html" not in asset.content_type
    license_response = client.get("/static/vendor/bootstrap-5.3.8/LICENSE")
    assert license_response.status_code == 200
    assert b"The MIT License (MIT)" in license_response.data


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
