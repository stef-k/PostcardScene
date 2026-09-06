"""Authenticated application settings form over shared persistence."""

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import login_required
from flask_wtf import FlaskForm
from sqlalchemy.exc import SQLAlchemyError
from wtforms import StringField
from wtforms.validators import InputRequired, ValidationError

from postcardscene.persistence import DatabaseError
from postcardscene.settings import get_timezone, set_timezone, validate_timezone

settings = Blueprint("settings", __name__)


class SettingsForm(FlaskForm):
    timezone = StringField(
        "Application timezone",
        validators=[
            InputRequired(
                message="Enter an IANA timezone, such as Europe/Athens or UTC."
            )
        ],
    )

    def validate_timezone(self, field):
        try:
            validate_timezone(field.data)
        except ValueError as error:
            raise ValidationError(str(error)) from error


@settings.route("/settings", methods=["GET", "POST"])
@login_required
def index():
    database = current_app.extensions["postcardscene.database"]
    form = SettingsForm()
    error = None
    try:
        if form.validate_on_submit():
            set_timezone(database, form.timezone.data)
            flash("Settings saved.")
            return redirect(url_for("settings.index"))
        if request.method == "GET":
            form.timezone.data = get_timezone(database)
    except (DatabaseError, SQLAlchemyError):
        error = "Settings could not be loaded or saved. Check database health and try again."
    return render_template(
        "settings.html", form=form, error=error
    ), 503 if error else 200
