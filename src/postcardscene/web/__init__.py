"""Flask control plane, independent of the runtime/player lifecycle."""

import os
from collections.abc import Mapping
from typing import Any

from flask import Flask, render_template
from werkzeug.exceptions import HTTPException, SecurityError

from postcardscene.persistence import DEFAULT_DATABASE_PATH
from postcardscene.web.control import control
from postcardscene.web.database import init_database


def create_app(config: Mapping[str, Any] | None = None) -> Flask:
    """Load defaults, optional operator-owned Python config, then explicit overrides."""
    app = Flask(__name__)
    app.config.from_mapping(
        DEBUG=False, TESTING=False, SECRET_KEY=None, DATABASE_PATH=DEFAULT_DATABASE_PATH
    )
    if "POSTCARDSCENE_CONFIG" in os.environ:
        app.config.from_envvar("POSTCARDSCENE_CONFIG")
    if config is not None:
        app.config.from_mapping(config)
    init_database(app)
    app.register_blueprint(control)
    app.register_error_handler(HTTPException, render_http_error)
    return app


def render_http_error(error: HTTPException):
    """Keep status/headers while excluding exception descriptions from HTML."""
    response = error.get_response()
    if isinstance(error, SecurityError):
        response.set_data("Bad request")
        response.content_type = "text/plain; charset=utf-8"
        return response
    response.data = render_template("error.html", code=error.code, name=error.name)
    response.content_type = "text/html; charset=utf-8"
    return response
