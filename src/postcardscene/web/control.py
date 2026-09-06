"""Navigation shell; feature blueprints are added by their owning issues."""

from flask import Blueprint, render_template
from flask_login import login_required

control = Blueprint("control", __name__)


@control.get("/")
@login_required
def index():
    return render_template("index.html")
