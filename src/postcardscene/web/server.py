"""Foreground production WSGI boundary; all settings are trusted host input."""

import ipaddress
import logging
import sys
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import timedelta

from flask import request
from waitress import serve

from postcardscene.configuration import load_operator_config
from postcardscene.web import create_app


def serving_options(config):
    """Validate production authority before constructing an app or listener."""
    mode = config.get("WEB_TRANSPORT_MODE", "direct_http")
    if mode not in ("direct_http", "reverse_proxy_https"):
        raise ValueError("Invalid transport mode.")
    host = config.get("WEB_BIND_HOST", "127.0.0.1")
    if not isinstance(host, str) or "%" in host:
        raise ValueError("Invalid bind address.")
    address = ipaddress.ip_address(host)
    port = config.get("WEB_BIND_PORT", 8080)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Invalid bind port.")
    insecure = config.get("WEB_ALLOW_INSECURE_REMOTE_HTTP", False)
    if type(insecure) is not bool:
        raise ValueError("Invalid HTTP opt-in.")
    proxy = mode == "reverse_proxy_https"
    if proxy and host != "127.0.0.1":
        raise ValueError("Invalid proxy bind.")
    if not proxy and not address.is_loopback and not insecure:
        raise ValueError("Remote HTTP requires opt-in.")
    hosts = config.get("TRUSTED_HOSTS")
    if isinstance(hosts, (str, bytes)) or not isinstance(hosts, Sequence) or not hosts:
        raise ValueError("Trusted Hosts required.")
    for value in hosts:
        if (
            not isinstance(value, str)
            or not value
            or any(char.isspace() or not char.isprintable() for char in value)
            or any(char in value for char in "/\\*?#@")
            or "://" in value
        ):
            raise ValueError("Invalid trusted Host.")
    options = dict(
        host=str(address),
        port=port,
        trusted_proxy="127.0.0.1" if proxy else None,
        trusted_proxy_count=1 if proxy else None,
        trusted_proxy_headers={
            "x-forwarded-for",
            "x-forwarded-proto",
            "x-forwarded-host",
        }
        if proxy
        else set(),
        clear_untrusted_proxy_headers=True,
        log_untrusted_proxy_headers=False,
        log_socket_errors=False,
        expose_tracebacks=False,
    )
    overrides = dict(
        TRUSTED_HOSTS=list(hosts),
        DEBUG=False,
        TESTING=False,
        SESSION_COOKIE_SECURE=proxy,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )
    return options, overrides


def production_app(config):
    options, overrides = serving_options(config)
    app = create_app(overrides, operator_config=config)
    if app.secret_key is None:
        raise ValueError("Signing authority unavailable.")
    if overrides["SESSION_COOKIE_SECURE"]:
        # Run before authentication/CSRF, including static and error requests.
        app.before_request_funcs[None].insert(0, require_https)
    return app, options


def require_https():
    if not request.is_secure:
        return "HTTPS required", 400, {"Content-Type": "text/plain; charset=utf-8"}


class SafeDiagnostic(logging.Formatter):
    def format(self, record):
        # Neither messages nor exception text from Flask/Waitress are public logs.
        return "Web server diagnostic."


@contextmanager
def safe_diagnostics():
    handler = logging.StreamHandler()
    handler.setFormatter(SafeDiagnostic())
    saved = []
    for name in ("waitress", "postcardscene.web"):
        logger = logging.getLogger(name)
        saved.append((logger, logger.handlers[:], logger.propagate, logger.level))
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
    try:
        yield
    finally:
        for logger, handlers, propagate, level in saved:
            logger.handlers = handlers
            logger.propagate = propagate
            logger.setLevel(level)
        handler.close()


def main() -> int:
    with safe_diagnostics():
        try:
            config = load_operator_config()
            app, options = production_app(config)
        except Exception:
            print("Web configuration/startup failed.", file=sys.stderr)
            return 1
        try:
            print("Web server starting.", file=sys.stderr)
            serve(app, **options)
        except Exception:
            print("Web server failed.", file=sys.stderr)
            return 1
        print("Web server stopped.", file=sys.stderr)
        return 0
