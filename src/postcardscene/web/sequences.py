"""Authenticated complete Sequence configuration; draft ordering never writes."""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.datastructures import MultiDict
from wtforms import FieldList, Form, FormField, IntegerField, SelectField
from wtforms.validators import InputRequired, NumberRange, Optional

from postcardscene import domain as d
from postcardscene.persistence import DatabaseError
from postcardscene.settings import read_playback_settings
from postcardscene.web.composition_forms import NamedForm, database, database_error

sequences = Blueprint("sequences", __name__, url_prefix="/sequences")
sequences.register_error_handler(DatabaseError, database_error)
sequences.register_error_handler(SQLAlchemyError, database_error)


class OccurrenceForm(Form):
    scene_id = SelectField("Scene", coerce=int, validators=[InputRequired()])
    duration_override_seconds = IntegerField(
        "Duration override in seconds (optional)",
        validators=[Optional(), NumberRange(min=1, max=86400)],
    )


class SequenceForm(NamedForm):
    mode = SelectField("Mode", choices=[("ordered", "Ordered"), ("shuffle", "Shuffle")])
    occurrences = FieldList(FormField(OccurrenceForm))


def existing_sequence(session, sequence_id):
    sequence = session.get(d.Sequence, sequence_id)
    if sequence is None:
        abort(404)
    return sequence


def sequence_data(session, sequence):
    return dict(
        id=sequence.id,
        name=sequence.name,
        mode=sequence.mode,
        enabled=sequence.enabled,
        occurrences=[
            dict(
                scene_id=row.scene_id,
                duration_override_seconds=row.duration_override_seconds,
                name=scene.name,
                layout=scene.layout,
                enabled=scene.enabled,
            )
            for row in d.list_sequence_memberships(session, sequence.id)
            for scene in [d.get_scene(session, row.scene_id)]
        ],
    )


@sequences.get("")
@login_required
def index():
    with database().transaction() as session:
        rows = [sequence_data(session, row) for row in d.list_sequences(session)]
        active = read_playback_settings(session).active_sequence_id
    return render_template("sequences.html", sequences=rows, active=active)


def change_draft(form, action):
    """Rebuild contiguous form rows, retaining raw invalid input for later validation."""
    rows = [
        {
            name: (field.raw_data or [""])[0]
            for name, field in entry.form._fields.items()
        }
        for entry in form.occurrences
    ]
    if action == "add":
        rows.append({"scene_id": "", "duration_override_seconds": ""})
    else:
        operation, separator, index = action.partition(":")
        if (
            not separator
            or not index.isdecimal()
            or operation not in ("remove", "up", "down")
        ):
            abort(400)
        index = int(index)
        if not 0 <= index < len(rows):
            abort(400)
        if operation == "remove":
            rows.pop(index)
        else:
            target = index + (-1 if operation == "up" else 1)
            if not 0 <= target < len(rows):
                abort(400)
            rows[index], rows[target] = rows[target], rows[index]
    data = MultiDict(
        (key, value)
        for key, value in request.form.items()
        if not key.startswith("occurrences-")
    )
    for index, row in enumerate(rows):
        for key, value in row.items():
            data.add(f"occurrences-{index}-{key}", value)
    return SequenceForm(formdata=data)


def save_sequence(form, sequence_id):
    with database().transaction(write=True) as session:
        if sequence_id is not None:
            existing_sequence(session, sequence_id)
        memberships = []
        for entry in form.occurrences:
            scene = d.get_scene(session, entry.scene_id.data)
            if scene.layout != "single":
                raise d.DomainError(
                    "Replace or remove unsupported split occurrences before saving."
                )
            memberships.append((scene.id, entry.duration_override_seconds.data))
        fields = dict(
            name=form.name.data,
            mode=form.mode.data,
            enabled=form.enabled.data,
            memberships=memberships,
        )
        if sequence_id is None:
            d.create_sequence(session, **fields)
        else:
            d.update_sequence(session, sequence_id, **fields)


@sequences.route("/new", methods=["GET", "POST"])
@sequences.route("/<int:sequence_id>/edit", methods=["GET", "POST"])
@login_required
def edit(sequence_id=None):
    form = SequenceForm()
    with database().transaction() as session:
        original = (
            sequence_data(session, existing_sequence(session, sequence_id))
            if sequence_id is not None
            else None
        )
        if request.method == "GET":
            form.process(data=original or {"occurrences": [{}], "enabled": True})
        if request.method == "POST" and request.form.get("action", "save") != "save":
            form = change_draft(form, request.form["action"])
        choices = [
            (row.id, row.name + (" — Disabled" if not row.enabled else ""))
            for row in d.list_scenes(session)
            if row.layout == "single"
        ]
        unsupported = {
            row["scene_id"]: row["name"]
            for row in (original or {}).get("occurrences", [])
            if row["layout"] != "single"
        }
        for entry in form.occurrences:
            entry.scene_id.choices = [(0, "Choose a single Scene"), *choices]
            if entry.scene_id.data in unsupported:
                entry.scene_id.choices.append(
                    (
                        entry.scene_id.data,
                        unsupported[entry.scene_id.data]
                        + " — Not executable in V0; replace or remove",
                    )
                )
    error = None
    if request.form.get("action", "save") == "save" and form.validate_on_submit():
        try:
            save_sequence(form, sequence_id)
        except d.DomainError:
            error = "Sequence could not be saved. Supply at least one single Scene occurrence; replace or remove unsupported split occurrences. Check all fields and try again."
        else:
            flash("Sequence saved.", "success")
            return redirect(url_for("sequences.index"))
    return render_template(
        "sequence_form.html", form=form, sequence_id=sequence_id, error=error
    )


@sequences.post("/<int:sequence_id>/delete")
@login_required
def delete(sequence_id):
    with database().transaction(write=True) as session:
        existing_sequence(session, sequence_id)
        active = read_playback_settings(session).active_sequence_id == sequence_id
        d.remove_sequence(session, sequence_id)
    flash(
        "Sequence deleted. Scenes, Widgets and Sources are preserved."
        + (
            " Active selection is now None / Idle; no replacement was selected."
            if active
            else ""
        ),
        "success",
    )
    return redirect(url_for("sequences.index"))
