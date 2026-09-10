"""Authenticated DB-only backup policy and advisory scheduled status."""

from datetime import UTC, datetime

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
from wtforms import BooleanField, IntegerField, StringField
from wtforms.validators import InputRequired, NumberRange

from postcardscene.backup_policy import get_backup_status, replace_backup_policy
from postcardscene.persistence import DatabaseError
from postcardscene.web.composition_forms import database

backup = Blueprint("backup", __name__, url_prefix="/backup")


class BackupForm(FlaskForm):
    enabled = BooleanField("Enable scheduled backups")
    destination_path = StringField("Destination absolute path")
    local_hour = IntegerField(
        "Local scheduled hour (0–23)",
        validators=[InputRequired(), NumberRange(min=0, max=23)],
    )
    retention_count = IntegerField(
        "Archives to retain (1–30)",
        validators=[InputRequired(), NumberRange(min=1, max=30)],
    )


def utc_now():
    return datetime.now(UTC)


@backup.errorhandler(DatabaseError)
@backup.errorhandler(SQLAlchemyError)
def database_unavailable(error):
    template = current_app.jinja_env.get_template("backup_error.html")
    return template.render(url_for=url_for), 503


def timestamp(value):
    return (
        datetime.fromtimestamp(value / 1_000_000_000, UTC).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
        if value is not None
        else "None recorded"
    )


@backup.route("", methods=["GET", "POST"])
@login_required
def index():
    now = utc_now()
    status = get_backup_status(database(), now, include_destination=True)
    form = BackupForm()
    if request.method == "POST" and form.validate_on_submit():
        try:
            replace_backup_policy(
                database(),
                form.enabled.data,
                form.destination_path.data or None,
                form.local_hour.data,
                form.retention_count.data,
            )
        except ValueError:
            form.destination_path.errors.append(
                "Use an absolute path of at most 4096 characters without NUL; "
                "a destination is required when enabled."
            )
        else:
            flash("Backup policy saved.", "success")
            return redirect(url_for("backup.index"))
    if request.method == "GET":
        form = BackupForm(
            formdata=None,
            data={
                "enabled": status.enabled,
                "destination_path": status.destination_path,
                "local_hour": status.local_hour,
                "retention_count": status.retention_count,
            },
        )
    return render_template("backup.html", form=form, status=status, timestamp=timestamp)
