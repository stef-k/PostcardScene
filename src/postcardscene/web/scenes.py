"""Authenticated single-Scene management preserving future split configuration."""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy.exc import SQLAlchemyError

from postcardscene import domain as d
from postcardscene.persistence import DatabaseError
from postcardscene.web.composition_forms import (
    SceneForm,
    choice_label,
    database,
    database_error,
)

scenes = Blueprint("scenes", __name__, url_prefix="/scenes")
scenes.register_error_handler(DatabaseError, database_error)
scenes.register_error_handler(SQLAlchemyError, database_error)


def existing_scene(session, scene_id, *, single=False):
    scene = session.get(d.Scene, scene_id)
    if scene is None:
        abort(404)
    if single and scene.layout != "single":
        abort(409)
    return scene


@scenes.get("")
@login_required
def index():
    with database().transaction() as session:
        rows = [
            dict(
                id=scene.id,
                name=scene.name,
                layout=scene.layout,
                enabled=scene.enabled,
                duration_seconds=scene.duration_seconds,
            )
            for scene in d.list_scenes(session)
        ]
    return render_template("scenes.html", scenes=rows)


def save_scene(form, scene_id):
    fields = dict(
        name=form.name.data,
        layout="single",
        placements=[("main", form.widget_id.data)],
        duration_seconds=form.duration_seconds.data,
        enabled=form.enabled.data,
    )
    with database().transaction(write=True) as session:
        if scene_id is None:
            d.create_scene(session, **fields)
        else:
            existing_scene(session, scene_id, single=True)
            d.update_scene(session, scene_id, **fields)


@scenes.route("/new", methods=["GET", "POST"])
@scenes.route("/<int:scene_id>/edit", methods=["GET", "POST"])
@login_required
def edit(scene_id=None):
    form = SceneForm()
    with database().transaction() as session:
        if scene_id is not None:
            scene = existing_scene(session, scene_id, single=True)
            if request.method == "GET":
                placement = d.list_scene_placements(session, scene_id)[0]
                form.process(
                    data=dict(
                        name=scene.name,
                        enabled=scene.enabled,
                        duration_seconds=scene.duration_seconds,
                        widget_id=placement.widget_id,
                    )
                )
        form.widget_id.choices = [
            (widget.id, choice_label(widget)) for widget in d.list_widgets(session)
        ]
    if form.validate_on_submit():
        try:
            save_scene(form, scene_id)
        except d.DomainError:
            form.widget_id.errors.append(
                "Scene could not be saved. Check the name, duration and Widget, then try again."
            )
        else:
            flash("Scene saved.", "success")
            return redirect(url_for("scenes.index"))
    return render_template("scene_form.html", form=form, scene_id=scene_id)


@scenes.post("/<int:scene_id>/delete")
@login_required
def delete(scene_id):
    try:
        with database().transaction(write=True) as session:
            existing_scene(session, scene_id)
            d.remove_scene(session, scene_id)
    except d.DomainError:
        flash(
            "Scene is referenced by a Sequence. Remove that membership before deleting the Scene.",
            "warning",
        )
    else:
        flash("Scene deleted. Widgets and Sources are preserved.", "success")
    return redirect(url_for("scenes.index"))
