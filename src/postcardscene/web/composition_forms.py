"""Small structured forms shared by Widget and single-Scene management."""

from flask import current_app, url_for
from flask_wtf import FlaskForm
from wtforms import BooleanField, IntegerField, SelectField, StringField
from wtforms.validators import InputRequired, Length, NumberRange, Optional


class NamedForm(FlaskForm):
    name = StringField(
        "Name",
        validators=[InputRequired(), Length(min=1, max=128)],
        filters=[lambda value: value.strip() if value else value],
    )
    enabled = BooleanField("Enabled", default=True)


class WidgetForm(NamedForm):
    source_id = SelectField("Source", coerce=int, validators=[InputRequired()])


class ImageWidgetForm(WidgetForm):
    fit = SelectField(
        "Fit",
        choices=[
            ("contain", "Contain (show entire image)"),
            ("cover", "Cover (center crop)"),
        ],
        default="contain",
        validators=[InputRequired()],
    )


class VideoWidgetForm(WidgetForm):
    audio_enabled = BooleanField("Enable audio", default=False)
    volume = IntegerField(
        "Volume",
        default=50,
        validators=[InputRequired(), NumberRange(min=0, max=100)],
    )


class SceneForm(NamedForm):
    widget_id = SelectField(
        "Widget for main region", coerce=int, validators=[InputRequired()]
    )
    duration_seconds = IntegerField(
        "Duration in seconds (optional)",
        validators=[Optional(), NumberRange(min=1, max=86400)],
    )


def choice_label(row):
    return f"{row.name} ({row.kind})" + (" — Disabled" if not row.enabled else "")


def database():
    return current_app.extensions["postcardscene.database"]


def database_error(error):
    # Login's DB lookup can fail too; do not invoke its context processor again.
    template = current_app.jinja_env.get_template("composition_error.html")
    return template.render(url_for=url_for), 503
