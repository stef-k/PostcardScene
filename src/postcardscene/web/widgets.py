"""Authenticated V0 Widget configuration; no content access or runtime work."""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy.exc import SQLAlchemyError

from postcardscene import domain as d
from postcardscene.image_selection import validate_image_configuration
from postcardscene.persistence import DatabaseError
from postcardscene.video_selection import validate_video_configuration
from postcardscene.web.composition_forms import (
    ImageWidgetForm,
    VideoWidgetForm,
    WidgetForm,
    choice_label,
    database,
    database_error,
)
from postcardscene.web_selection import validate_web_configuration

widgets = Blueprint("widgets", __name__, url_prefix="/widgets")
widgets.register_error_handler(DatabaseError, database_error)
widgets.register_error_handler(SQLAlchemyError, database_error)
KINDS = {
    "image": "Image",
    "portrait_image_pair": "Portrait pair",
    "video": "Video",
    "web_view": "Web view",
}


def managed_widget(session, widget_id):
    widget = session.get(d.Widget, widget_id)
    if widget is None or widget.kind not in KINDS:
        abort(404)
    return widget


def source_kinds(kind):
    return (
        {"web_url"} if kind == "web_view" else {"local_directory", "mounted_directory"}
    )


def widget_form(kind):
    if kind in ("image", "portrait_image_pair"):
        return ImageWidgetForm()
    if kind == "video":
        return VideoWidgetForm()
    return WidgetForm()


def configuration(form, kind):
    if kind in ("image", "portrait_image_pair"):
        return validate_image_configuration(kind, {"fit": form.fit.data})
    if kind == "video":
        return validate_video_configuration(
            kind, {"audio_enabled": form.audio_enabled.data, "volume": form.volume.data}
        )
    return validate_web_configuration(kind, {})


@widgets.get("")
@login_required
def index():
    with database().transaction() as session:
        rows = []
        for widget in d.list_widgets(session):
            source = (
                session.get(d.Source, widget.source_id) if widget.source_id else None
            )
            rows.append(
                dict(
                    id=widget.id,
                    name=widget.name,
                    kind=widget.kind,
                    enabled=widget.enabled,
                    source=choice_label(source) if source else "No Source",
                )
            )
    return render_template("widgets.html", widgets=rows, kinds=KINDS)


def save_widget(form, kind, widget_id):
    fields = dict(
        name=form.name.data,
        kind=kind,
        configuration=configuration(form, kind),
        enabled=form.enabled.data,
        source_id=form.source_id.data,
    )
    with database().transaction(write=True) as session:
        if widget_id is not None:
            if managed_widget(session, widget_id).kind != kind:
                abort(409)
        source = session.get(d.Source, form.source_id.data)
        if source is None or source.kind not in source_kinds(kind):
            form.source_id.errors.append("Choose an existing compatible Source.")
            return False
        if widget_id is None:
            d.create_widget(session, **fields)
        else:
            d.update_widget(session, widget_id, **fields)
    return True


@widgets.route("/new/<kind>", methods=["GET", "POST"])
@widgets.route("/<int:widget_id>/edit", methods=["GET", "POST"])
@login_required
def edit(kind=None, widget_id=None):
    with database().transaction() as session:
        widget = managed_widget(session, widget_id) if widget_id is not None else None
        kind = widget.kind if widget is not None else kind
        if kind not in KINDS:
            abort(404)
        form = widget_form(kind)
        if widget is not None and request.method == "GET":
            form.process(
                data=dict(
                    widget.configuration,
                    name=widget.name,
                    source_id=widget.source_id,
                    enabled=widget.enabled,
                )
            )
        form.source_id.choices = [
            (source.id, choice_label(source))
            for source in d.list_sources(session)
            if source.kind in source_kinds(kind)
        ]
    if form.validate_on_submit():
        try:
            saved = save_widget(form, kind, widget_id)
        except d.DomainError:
            form.name.errors.append(
                "Widget could not be saved. Check the name and Source, then try again."
            )
        else:
            if saved:
                flash("Widget saved.", "success")
                return redirect(url_for("widgets.index"))
    return render_template(
        "widget_form.html",
        form=form,
        kind=kind,
        kind_label=KINDS[kind],
        widget_id=widget_id,
    )


@widgets.post("/<int:widget_id>/delete")
@login_required
def delete(widget_id):
    try:
        with database().transaction(write=True) as session:
            managed_widget(session, widget_id)
            d.remove_widget(session, widget_id)
    except d.DomainError:
        flash(
            "Widget is referenced by a Scene. Reassign or remove that reference before deleting the Widget.",
            "warning",
        )
    else:
        flash("Widget deleted. Sources and media are preserved.", "success")
    return redirect(url_for("widgets.index"))
