"""Authenticated filesystem Source management over domain and durable request seams."""

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
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from wtforms import BooleanField, SelectField, StringField
from wtforms.validators import InputRequired, Length

from postcardscene.catalog_requests import request_catalog_reconciliation
from postcardscene.domain import (
    DomainError,
    Source,
    create_source,
    remove_source,
    update_source,
)
from postcardscene.filesystem_source import InvalidSource, PathPolicy, validate_source
from postcardscene.persistence import DatabaseError
from postcardscene.settings import get_timezone
from postcardscene.web.source_health import KINDS, source_view

sources = Blueprint("sources", __name__, url_prefix="/sources")


class SourceForm(FlaskForm):
    name = StringField(
        "Name",
        validators=[InputRequired(), Length(min=1, max=128)],
        filters=[lambda value: value.strip() if value else value],
    )
    kind = SelectField(
        "Kind", choices=list(KINDS.items()), validators=[InputRequired()]
    )
    path = StringField("Path", validators=[InputRequired(), Length(max=4096)])
    recursive = BooleanField("Include subdirectories", default=True)
    enabled = BooleanField("Enabled", default=True)


def database():
    return current_app.extensions["postcardscene.database"]


@sources.errorhandler(DatabaseError)
@sources.errorhandler(SQLAlchemyError)
def database_error(error):
    # Also covers database failure while login_required loads the administrator.
    template = current_app.jinja_env.get_template("sources_error.html")
    # Skip Flask-Login context processors: the user lookup itself may have failed.
    return template.render(url_for=url_for), 503


def managed_source(session, source_id):
    source = session.get(Source, source_id)
    if source is None or source.kind not in KINDS:
        abort(404)
    return source


@sources.get("")
@login_required
def index():
    timezone = ZoneInfo(get_timezone(database()))
    with database().transaction() as session:
        rows = [
            source_view(session, source, timezone)
            for source in session.scalars(
                select(Source).where(Source.kind.in_(KINDS)).order_by(Source.id)
            )
        ]
    return render_template("sources.html", sources=rows)


def queue_refresh(source_id):
    try:
        request_catalog_reconciliation(database(), source_id)
    except InvalidSource:
        flash(
            "Refresh could not be queued. Choose an existing enabled filesystem Source.",
            "warning",
        )
    else:
        flash(
            "Catalog refresh queued. The runtime will process the request.", "success"
        )


def save_source(form, configuration, source_id):
    """Read the current edit intent under the existing short writer transaction."""
    fields = dict(
        name=form.name.data,
        kind=form.kind.data,
        configuration=configuration,
        enabled=form.enabled.data,
    )
    with database().transaction(write=True) as session:
        if source_id is None:
            source_id = create_source(session, **fields).id
            refresh = form.enabled.data
        else:
            source = managed_source(session, source_id)
            authority_changed = source.kind != form.kind.data or any(
                source.configuration.get(key) != configuration[key]
                for key in ("path", "recursive")
            )
            refresh = form.enabled.data and (authority_changed or not source.enabled)
            update_source(session, source_id, **fields)
    flash("Source saved.", "success")
    if refresh:
        try:
            queue_refresh(source_id)
        except (DatabaseError, SQLAlchemyError):
            flash(
                "Source saved, but catalog refresh could not be queued. Retry with Refresh.",
                "warning",
            )
    return redirect(url_for("sources.index"))


@sources.route("/new", methods=["GET", "POST"])
@sources.route("/<int:source_id>/edit", methods=["GET", "POST"])
@login_required
def edit(source_id=None):
    form = SourceForm()
    if source_id is not None:
        with database().transaction() as session:
            source = managed_source(session, source_id)
            if request.method == "GET":
                form.process(
                    data=dict(
                        name=source.name,
                        kind=source.kind,
                        path=source.configuration.get("path", ""),
                        recursive=source.configuration.get("recursive", False),
                        enabled=source.enabled,
                    )
                )
    try:
        policy = PathPolicy(current_app.config["MEDIA_ALLOWED_ROOTS"])
    except InvalidSource:
        policy = None
    roots = policy.allowed_roots if policy else ()
    if form.validate_on_submit():
        if not roots:
            form.path.errors.append(
                "No allowed media roots are configured. Ask the host administrator to configure MEDIA_ALLOWED_ROOTS."
            )
        else:
            try:
                # Canonical path validation may touch ancestors; keep it outside DB transactions.
                configuration = validate_source(
                    form.kind.data,
                    dict(path=form.path.data, recursive=form.recursive.data),
                    policy,
                )
            except InvalidSource:
                form.path.errors.append(
                    "Enter an absolute directory path within an allowed root, without parent traversal or a symlink escape."
                )
            else:
                try:
                    return save_source(form, configuration, source_id)
                except DomainError:
                    form.name.errors.append(
                        "Source could not be saved. Check the name and configuration."
                    )
    return render_template(
        "source_form.html", form=form, source_id=source_id, roots=roots
    )


@sources.post("/<int:source_id>/refresh")
@login_required
def refresh(source_id):
    queue_refresh(source_id)
    return redirect(url_for("sources.index"))


@sources.post("/<int:source_id>/delete")
@login_required
def delete(source_id):
    try:
        with database().transaction(write=True) as session:
            managed_source(session, source_id)
            remove_source(session, source_id)
    except DomainError:
        flash(
            "Source is referenced by a Widget. Reassign or remove that reference before deleting the Source.",
            "warning",
        )
    else:
        flash(
            "Source and its derived catalog deleted. Original media files were not changed.",
            "success",
        )
    return redirect(url_for("sources.index"))
