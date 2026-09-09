"""Production settings and the real Waitress-to-Flask trust boundary."""

import logging
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from flask import request, session
from waitress.server import create_server
from werkzeug.test import Client

from postcardscene.session_secret import DEFAULT_SECRET_PATH
from postcardscene.web import server


@pytest.mark.parametrize(
    "change",
    [
        {"WEB_TRANSPORT_MODE": "https"},
        {"WEB_BIND_HOST": "localhost"},
        {"WEB_BIND_HOST": 123},
        {"WEB_BIND_HOST": "127.0.0.1:8080"},
        {"WEB_BIND_PORT": True},
        {"WEB_BIND_PORT": "8080"},
        {"WEB_BIND_PORT": 1023},
        {"WEB_BIND_PORT": 65536},
        {"WEB_ALLOW_INSECURE_REMOTE_HTTP": 1},
        {"WEB_BIND_HOST": "0.0.0.0"},
        {"WEB_BIND_HOST": "::"},
        {"WEB_BIND_HOST": "192.0.2.1"},
        {"WEB_TRANSPORT_MODE": "reverse_proxy_https", "WEB_BIND_HOST": "::1"},
        {
            "WEB_TRANSPORT_MODE": "reverse_proxy_https",
            "WEB_BIND_HOST": "0.0.0.0",
            "WEB_ALLOW_INSECURE_REMOTE_HTTP": True,
        },
        {"TRUSTED_HOSTS": None},
        {"TRUSTED_HOSTS": []},
        {"TRUSTED_HOSTS": "localhost"},
        {"TRUSTED_HOSTS": [42]},
        {"TRUSTED_HOSTS": [""]},
        {"TRUSTED_HOSTS": ["bad host"]},
        {"TRUSTED_HOSTS": ["host\x00"]},
        {"TRUSTED_HOSTS": ["https://host"]},
        {"TRUSTED_HOSTS": ["host/path"]},
        {"TRUSTED_HOSTS": ["*.example.com"]},
    ],
)
def test_invalid_production_settings(change):
    with pytest.raises(ValueError):
        server.serving_options({"TRUSTED_HOSTS": ["localhost"], **change})


def test_defaults_and_explicit_lan_opt_in():
    options, overrides = server.serving_options({"TRUSTED_HOSTS": [".example.com"]})
    assert (options["host"], options["port"]) == ("127.0.0.1", 8080)
    assert options["trusted_proxy"] is None
    assert options["trusted_proxy_headers"] == set()
    assert overrides["SESSION_COOKIE_SECURE"] is False
    for host in ("::1", "0.0.0.0", "::", "192.0.2.1"):
        options, _ = server.serving_options(
            {
                "TRUSTED_HOSTS": ["localhost"],
                "WEB_BIND_HOST": host,
                "WEB_ALLOW_INSECURE_REMOTE_HTTP": True,
            }
        )
        assert options["host"] == host
    with pytest.raises(ValueError):
        server.serving_options({"SERVER_NAME": "localhost"})
    assert DEFAULT_SECRET_PATH == Path("/var/lib/postcardscene-web/session.key")


@contextmanager
def boundary(mode):
    app, options = server.production_app(
        {
            "TRUSTED_HOSTS": ["control.example", "localhost"],
            "WEB_TRANSPORT_MODE": mode,
            "SESSION_COOKIE_SECURE": mode == "direct_http",
            "SESSION_COOKIE_HTTPONLY": False,
            "SESSION_COOKIE_SAMESITE": "None",
            "DEBUG": True,
            "TESTING": True,
        }
    )

    @app.get("/probe")
    def probe():
        session["probe"] = True
        return {
            "client": request.remote_addr,
            "scheme": request.scheme,
            "host": request.host,
            "forwarded": request.headers.get("Forwarded"),
            "port": request.headers.get("X-Forwarded-Port"),
        }

    assert not app.debug and not app.testing
    assert app.permanent_session_lifetime == timedelta(hours=12)
    # Ephemeral test port; use the real server's configured WSGI boundary to
    # exercise both trusted and non-loopback peers without host provisioning.
    listener = create_server(app, **{**options, "port": 0})
    try:
        yield Client(listener.application), app
    finally:
        listener.task_dispatcher.shutdown()
        listener.close()
        app.extensions["postcardscene.database"].engine.dispose()


FORWARDED = {
    "Forwarded": "for=198.51.100.1;proto=https;host=evil.example",
    "X-Forwarded-For": "198.51.100.1, 192.0.2.2",
    "X-Forwarded-Proto": "https",
    "X-Forwarded-Host": "control.example",
    "X-Forwarded-Port": "4444",
}


def test_direct_ignores_forwarded_authority_and_forces_http_cookie():
    with boundary("direct_http") as (client, _):
        response = client.get(
            "/probe", headers=FORWARDED, environ_overrides={"REMOTE_ADDR": "127.0.0.1"}
        )
        assert response.json == {
            "client": "127.0.0.1",
            "scheme": "http",
            "host": "localhost",
            "forwarded": None,
            "port": None,
        }
        cookie = response.headers["Set-Cookie"]
        assert "Secure" not in cookie
        assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
        assert (
            client.get(
                "/probe",
                headers={"Host": "evil.example"},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            ).status_code
            == 400
        )


def test_proxy_trusts_only_one_local_hop_and_external_host():
    with boundary("reverse_proxy_https") as (client, _):
        response = client.get(
            "/probe", headers=FORWARDED, environ_overrides={"REMOTE_ADDR": "127.0.0.1"}
        )
        assert response.json == {
            "client": "192.0.2.2",
            "scheme": "https",
            "host": "control.example",
            "forwarded": None,
            "port": None,
        }
        cookie = response.headers["Set-Cookie"]
        assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
        for headers, peer in (
            (FORWARDED, "192.0.2.9"),
            ({}, "127.0.0.1"),
            ({**FORWARDED, "X-Forwarded-Proto": "http"}, "127.0.0.1"),
        ):
            response = client.get(
                "/probe", headers=headers, environ_overrides={"REMOTE_ADDR": peer}
            )
            assert response.status_code == 400
            assert response.text == "HTTPS required"
        response = client.get(
            "/probe",
            headers={**FORWARDED, "X-Forwarded-Host": "evil.example"},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert response.status_code == 400


def test_entrypoint_reads_config_once_and_foregrounds_server(
    tmp_path, monkeypatch, capsys
):
    config = tmp_path / "operator.py"
    marker = tmp_path / "reads"
    config.write_text(
        f"with open({str(marker)!r}, 'a') as f: f.write('read')\n"
        "TRUSTED_HOSTS = ['localhost']\n"
    )
    monkeypatch.setenv("POSTCARDSCENE_CONFIG", str(config))
    calls = []
    monkeypatch.setattr(server, "serve", lambda app, **options: calls.append(options))
    assert server.main() == 0
    assert marker.read_text() == "read"
    assert calls[0]["host"] == "127.0.0.1"
    assert "Web server stopped." in capsys.readouterr().err


def test_startup_and_waitress_diagnostics_are_sanitized(tmp_path, monkeypatch, capsys):
    config = tmp_path / "operator.py"
    config.write_text("raise ValueError('PRIVATE CREDENTIAL')")
    monkeypatch.setenv("POSTCARDSCENE_CONFIG", str(config))
    assert server.main() == 1
    assert capsys.readouterr().err == "Web configuration/startup failed.\n"
    config.write_text("TRUSTED_HOSTS = ['localhost']\n")

    def fail(app, **options):
        try:
            raise RuntimeError("PRIVATE CREDENTIAL")
        except RuntimeError:
            logging.getLogger("waitress").exception("private URL")
            logging.getLogger("postcardscene.web").exception("private request")
            raise

    monkeypatch.setattr(server, "serve", fail)
    assert server.main() == 1
    output = capsys.readouterr().err
    assert "PRIVATE" not in output and "private" not in output
    assert "Web server failed." in output
