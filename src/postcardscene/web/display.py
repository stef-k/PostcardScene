"""Authenticated display policy and fixed local panel diagnostics."""

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
from wtforms import IntegerField, SelectField
from wtforms.validators import InputRequired, NumberRange

from postcardscene import panel_client
from postcardscene.persistence import DatabaseError
from postcardscene.settings import (
    get_display_power_settings,
    set_display_power_settings,
)

display = Blueprint("display", __name__)


class DisplayForm(FlaskForm):
    display_power_backend = SelectField(
        "Backend",
        choices=[
            ("auto", "Automatic"),
            ("cec", "HDMI-CEC"),
            ("ddc", "DDC/CI"),
            ("signal", "Signal only"),
        ],
    )
    display_wake_delay_seconds = IntegerField(
        "Wake delay seconds", validators=[InputRequired(), NumberRange(min=0, max=30)]
    )
    maximum_static_dwell_seconds = IntegerField(
        "Maximum static dwell seconds",
        validators=[InputRequired(), NumberRange(min=300, max=14400)],
    )


@display.route("/display", methods=["GET", "POST"])
@login_required
def index():
    database = current_app.extensions["postcardscene.database"]
    form = DisplayForm()
    error = None
    policy = None
    try:
        if form.validate_on_submit():
            set_display_power_settings(
                database,
                form.display_power_backend.data,
                form.display_wake_delay_seconds.data,
                form.maximum_static_dwell_seconds.data,
            )
            flash("Display policy saved.")
            return redirect(url_for("display.index"))
        policy = get_display_power_settings(database)
        if request.method == "GET":
            form = DisplayForm(obj=policy)
    except (DatabaseError, SQLAlchemyError):
        error = "Display policy could not be loaded or saved. Check database health and try again."
    live = panel_client.read_status()
    return render_template(
        "display.html",
        form=form,
        test_form=FlaskForm(),
        policy=policy,
        live=live,
        error=error,
    ), 503 if error else 200


@display.post("/display/test-wake")
@display.post("/display/test-sleep", endpoint="test_sleep")
@login_required
def test_wake():
    form = FlaskForm()
    if form.validate_on_submit():
        wake = request.endpoint == "display.test_wake"
        result = panel_client.test_wake() if wake else panel_client.test_sleep()
        feedback = {
            "accepted": "Diagnostic test submitted for five seconds; this does not confirm a power transition. Refresh live status to check convergence.",
            "unavailable": "Runtime panel status unavailable. Diagnostic test could not be confirmed.",
            "rejected": "Diagnostic test rejected. Test wake cannot override panel protection.",
            "cleanup_failed": "Runtime panel failure. Diagnostic test cannot proceed; runtime recovery is required.",
        }
        flash(feedback[result.outcome])
    return redirect(url_for("display.index"))
