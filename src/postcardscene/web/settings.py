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
from wtforms import IntegerField, SelectField, StringField
from wtforms.validators import InputRequired, NumberRange, ValidationError

from postcardscene import domain as d
from postcardscene.persistence import DatabaseError
from postcardscene.settings import (
    get_playback_settings,
    get_timezone,
    set_active_sequence,
    set_default_scene_dwell,
    set_timezone,
    validate_timezone,
)

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


class ActiveSequenceForm(FlaskForm):
    active_sequence_id = SelectField("Active Sequence", coerce=int)


class DwellForm(FlaskForm):
    default_scene_dwell_seconds = IntegerField(
        "Fallback Scene dwell in seconds",
        validators=[InputRequired(), NumberRange(min=1, max=86400)],
    )


@settings.route("/settings", methods=["GET", "POST"])
@settings.post("/settings/active-sequence", endpoint="active_sequence")
@settings.post("/settings/default-dwell", endpoint="default_dwell")
@login_required
def index():
    database = current_app.extensions["postcardscene.database"]
    form = SettingsForm()
    active_form = ActiveSequenceForm()
    dwell_form = DwellForm()
    error = None
    try:
        playback = get_playback_settings(database)
        with database.transaction() as session:
            active_form.active_sequence_id.choices = [(0, "None / Idle")] + [
                (row.id, row.name + (" — Disabled" if not row.enabled else ""))
                for row in d.list_sequences(session)
            ]
        if (
            request.endpoint == "/settings/active-sequence"
            and active_form.validate_on_submit()
        ):
            try:
                set_active_sequence(
                    database, active_form.active_sequence_id.data or None
                )
            except ValueError:
                active_form.active_sequence_id.errors.append(
                    "Sequence no longer exists. Choose again."
                )
            else:
                flash("Active selection saved.")
                return redirect(url_for("settings.index"))
        elif (
            request.endpoint == "/settings/default-dwell"
            and dwell_form.validate_on_submit()
        ):
            set_default_scene_dwell(
                database, dwell_form.default_scene_dwell_seconds.data
            )
            flash("Fallback dwell saved.")
            return redirect(url_for("settings.index"))
        elif request.endpoint == "settings.index" and form.validate_on_submit():
            set_timezone(database, form.timezone.data)
            flash("Settings saved.")
            return redirect(url_for("settings.index"))
        if request.endpoint != "settings.index" or request.method == "GET":
            form.timezone.data = get_timezone(database)
        if request.endpoint != "settings.active_sequence":
            active_form.active_sequence_id.data = playback.active_sequence_id or 0
        if request.endpoint != "settings.default_dwell":
            dwell_form.default_scene_dwell_seconds.data = (
                playback.default_scene_dwell_seconds
            )
    except (DatabaseError, SQLAlchemyError):
        error = "Settings could not be loaded or saved. Check database health and try again."
    return render_template(
        "settings.html",
        form=form,
        active_form=active_form,
        dwell_form=dwell_form,
        error=error,
    ), 503 if error else 200
