"""Authenticated appliance overview."""

from flask import Blueprint, current_app, render_template
from flask_login import login_required

from postcardscene.status import dashboard_status

control = Blueprint("control", __name__)


@control.get("/")
@login_required
def index():
    status = dashboard_status(current_app.extensions["postcardscene.database"])
    return render_template("index.html", status=status)
