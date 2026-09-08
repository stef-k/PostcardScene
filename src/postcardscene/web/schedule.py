"""Authenticated schedule configuration; requests only read or write durable state."""

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from flask import (
    Blueprint,
    abort,
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
from werkzeug.datastructures import MultiDict
from wtforms import (
    BooleanField,
    FieldList,
    Form,
    FormField,
    IntegerField,
    SelectField,
    StringField,
)
from wtforms.validators import InputRequired, NumberRange, Regexp, ValidationError

from postcardscene.operating_schedule import (
    MAX_OVERRIDE_MINUTES,
    MAX_WINDOWS,
    WeeklyWindow,
    clear_temporary_override,
    evaluate_operating_schedule,
    get_operating_schedule,
    replace_operating_schedule,
    set_temporary_override,
)
from postcardscene.persistence import DatabaseError
from postcardscene.web.composition_forms import database

schedule = Blueprint("schedule", __name__, url_prefix="/schedule")
WEEKDAYS = tuple(
    enumerate(
        ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    )
)
TIME_PATTERN = r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z"


def minute(value):
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


class WindowForm(Form):
    weekday = SelectField("Weekday", choices=WEEKDAYS, coerce=int)
    start = StringField(
        "Start (HH:MM)",
        validators=[
            Regexp(TIME_PATTERN, message="Enter a time from 00:00 to 23:59 (HH:MM).")
        ],
    )
    end = StringField(
        "End (HH:MM; 00:00 = end of day)",
        validators=[
            Regexp(
                TIME_PATTERN,
                message="Enter a time from 00:00 to 23:59; 00:00 means end of day.",
            )
        ],
    )

    def validate_end(self, field):
        if re.fullmatch(TIME_PATTERN, self.start.data or "") and re.fullmatch(
            TIME_PATTERN, field.data or ""
        ):
            if minute(self.start.data) >= (minute(field.data) or 1440):
                raise ValidationError(
                    "End must follow start. Enter overnight activity as two windows on adjacent days, ending the first at 00:00."
                )


class ScheduleForm(FlaskForm):
    enabled = BooleanField("Enable weekly schedule")
    # Do not truncate submissions at max_entries: an oversized save must fail whole.
    windows = FieldList(FormField(WindowForm))

    def validate_windows(self, field):
        if len(field.entries) > MAX_WINDOWS:
            raise ValidationError(
                "Supply at most 64 weekly windows. Remove a window before saving."
            )


class OverrideForm(FlaskForm):
    state = SelectField(
        "Temporary state", choices=[("active", "Keep active"), ("sleep", "Sleep")]
    )
    duration_minutes = IntegerField(
        "Duration in minutes",
        default=60,
        validators=[InputRequired(), NumberRange(min=1, max=MAX_OVERRIDE_MINUTES)],
    )


def utc_now():
    return datetime.now(UTC)


@schedule.errorhandler(DatabaseError)
@schedule.errorhandler(SQLAlchemyError)
def database_unavailable(error):
    # Avoid retrying the login DB lookup through template context processors.
    template = current_app.jinja_env.get_template("schedule_error.html")
    return template.render(url_for=url_for), 503


def change_draft(form, action):
    rows = [
        {field.short_name: (field.raw_data or [""])[0] for field in entry.form}
        for entry in form.windows
    ]
    if action == "add":
        if len(rows) >= MAX_WINDOWS:
            return (
                form,
                "Supply at most 64 weekly windows. Remove a window before adding another.",
            )
        rows.append({"weekday": "0", "start": "", "end": ""})
    else:
        operation, separator, index = action.partition(":")
        if (
            operation != "remove"
            or not separator
            or not index.isdecimal()
            or len(index) > len(str(len(rows)))
        ):
            abort(400)
        index = int(index)
        if not 0 <= index < len(rows):
            abort(400)
        rows.pop(index)
    data = MultiDict(
        (key, value)
        for key, value in request.form.items()
        if not key.startswith("windows-")
    )
    for index, row in enumerate(rows):
        for key, value in row.items():
            data.add(f"windows-{index}-{key}", value)
    return ScheduleForm(formdata=data), None


def saved_form(snapshot):
    return ScheduleForm(
        formdata=None,
        data=dict(
            enabled=snapshot.enabled,
            windows=[
                dict(
                    weekday=w.weekday,
                    start=f"{w.start_minute // 60:02}:{w.start_minute % 60:02}",
                    end=f"{w.end_minute % 1440 // 60:02}:{w.end_minute % 60:02}",
                )
                for w in snapshot.windows
            ],
        ),
    )


def show_page(form=None, override_form=None, error=None):
    snapshot = get_operating_schedule(database())
    decision = evaluate_operating_schedule(snapshot, utc_now())
    expiry = None
    if snapshot.override_until_utc is not None:
        try:
            expiry = (
                datetime.fromtimestamp(snapshot.override_until_utc, UTC)
                .astimezone(ZoneInfo(snapshot.timezone))
                .strftime("%Y-%m-%d %H:%M:%S %Z (%z)")
            )
        except (OverflowError, OSError, ValueError) as cause:
            raise DatabaseError("Override expiry cannot be displayed.") from cause
    return render_template(
        "schedule.html",
        form=form if form is not None else saved_form(snapshot),
        override_form=override_form
        if override_form is not None
        else OverrideForm(formdata=None),
        snapshot=snapshot,
        decision=decision,
        expiry=expiry,
        error=error,
        reasons={
            "schedule_disabled": "schedule disabled",
            "schedule": "weekly schedule",
            "override": "temporary override",
        },
    )


@schedule.route("", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "GET":
        return show_page()
    form = ScheduleForm()
    action = request.form.get("action", "save")
    if action != "save":
        form, error = change_draft(form, action)
        return show_page(form=form, error=error)
    if form.validate_on_submit():
        windows = tuple(
            WeeklyWindow(
                entry.weekday.data,
                minute(entry.start.data),
                minute(entry.end.data) or 1440,
            )
            for entry in form.windows
        )
        replace_operating_schedule(database(), form.enabled.data, windows)
        flash("Schedule saved.", "success")
        return redirect(url_for("schedule.index"))
    return show_page(form=form)


@schedule.post("/override")
@login_required
def override():
    form = OverrideForm()
    if form.validate_on_submit():
        set_temporary_override(
            database(),
            form.state.data == "active",
            form.duration_minutes.data,
            now=utc_now(),
        )
        flash("Temporary override saved.", "success")
        return redirect(url_for("schedule.index"))
    return show_page(override_form=form)


@schedule.post("/resume")
@login_required
def resume():
    clear_temporary_override(database())
    flash("Temporary override cleared.", "success")
    return redirect(url_for("schedule.index"))
