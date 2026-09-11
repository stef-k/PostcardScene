"""Control-origin headers and the local asset inventory."""

import re
from pathlib import Path

import pytest

from postcardscene.web import create_app


@pytest.mark.parametrize(
    "path,status",
    [("/login", 200), ("/", 302), ("/missing", 404), ("/static/control.js", 200)],
)
def test_response_headers(path, status):
    response = create_app({"TESTING": True}).test_client().get(path)
    assert response.status_code == status
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert (
        response.headers["Permissions-Policy"]
        == "camera=(), microphone=(), geolocation=(), payment=()"
    )
    assert "Strict-Transport-Security" not in response.headers
    directives = dict(
        part.strip().split(" ", 1)
        for part in response.headers["Content-Security-Policy"].split(";")
    )
    for name in (
        "default-src",
        "script-src",
        "style-src",
        "form-action",
        "font-src",
        "connect-src",
    ):
        assert directives[name] == "'self'"
    for name in (
        "object-src",
        "base-uri",
        "frame-ancestors",
        "frame-src",
        "worker-src",
    ):
        assert directives[name] == "'none'"
    assert directives["img-src"] == "'self' data:"
    if not path.startswith("/static/"):
        assert response.headers["Cache-Control"] == "no-store"


def test_templates_and_local_assets_need_no_inline_execution():
    root = Path("src/postcardscene/web")
    for template in (root / "templates").glob("*.html"):
        text = template.read_text()
        assert not re.search(r"<style\b|\sstyle=|\son\w+=", text)
        for script in re.findall(r"<script\b([^>]*)>(.*?)</script>", text, re.S):
            assert "url_for('static'" in script[0] and not script[1].strip()
    client = create_app({"TESTING": True}).test_client()
    page = client.get("/login").text
    assets = re.findall(r'(?:src|href)="(/static/[^"]+)"', page)
    assert len(assets) == 4
    for asset in assets:
        assert client.get(asset).status_code == 200
    css = (root / "static/vendor/bootstrap-5.3.8/bootstrap.min.css").read_text()
    assert "data:image/svg+xml" in css


def test_authenticated_and_error_responses(sources_ui):
    app, client, *_ = sources_ui
    expected = client.get("/login").headers["Content-Security-Policy"]
    for response in (
        client.get("/"),
        client.post("/logout"),
        client.get("/missing"),
        client.post("/login", data=b"x" * 17000),
    ):
        assert response.headers["Content-Security-Policy"] == expected
        assert response.headers["Cache-Control"] == "no-store"
        assert "Strict-Transport-Security" not in response.headers
    app.config["TRUSTED_HOSTS"] = ["localhost"]
    assert (
        client.get("/", headers={"Host": "evil.example"}).headers[
            "Content-Security-Policy"
        ]
        == expected
    )
