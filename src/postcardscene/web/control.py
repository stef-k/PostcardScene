"""Navigation shell; feature blueprints are added by their owning issues."""

from flask import Blueprint, render_template

control = Blueprint("control", __name__)


@control.get("/")
def index():
    return render_template("index.html")
