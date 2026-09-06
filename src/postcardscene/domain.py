"""Composition configuration operations within Database.transaction() sessions.

Use these operations for application writes, not direct ORM attribute assignment.
Configuration is replaced in full; it must not contain credentials, catalog/media
payloads or runtime state. Feature owners add path/URL semantics before use.
"""

import json
import math

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    String,
    UniqueConstraint,
    delete,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from postcardscene.catalog import supersede_catalog
from postcardscene.persistence import Base

SOURCE_KINDS = frozenset({"local_directory", "mounted_directory", "web_url"})
WIDGET_KINDS = frozenset({"image", "portrait_image_pair", "video", "web_view"})
MAX_NAME_LENGTH = 128
MAX_CONFIGURATION_BYTES = 16 * 1024


class DomainError(ValueError):
    """Invalid domain input or a lifecycle operation that cannot be completed."""


class Source(Base):
    __tablename__ = "source"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(MAX_NAME_LENGTH))
    kind: Mapped[str] = mapped_column(String(64))
    configuration: Mapped[dict] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean)


class Widget(Base):
    __tablename__ = "widget"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(MAX_NAME_LENGTH))
    kind: Mapped[str] = mapped_column(String(64))
    configuration: Mapped[dict] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("source.id", ondelete="RESTRICT"), index=True
    )


def _validate_json(value):
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise DomainError("Configuration keys must be strings.")
            _validate_json(item)
    elif type(value) is list:
        for item in value:
            _validate_json(item)
    elif type(value) is float:
        if not math.isfinite(value):
            raise DomainError("Configuration numbers must be finite.")
    elif value is not None and type(value) not in (str, int, bool):
        raise DomainError("Configuration must contain only JSON-native values.")


def validate_configuration(value):
    """Return an independent JSON object, bounded by compact UTF-8 encoding."""
    if type(value) is not dict:
        raise DomainError("Configuration must be a JSON object.")
    try:
        _validate_json(value)
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        if len(encoded.encode("utf-8")) > MAX_CONFIGURATION_BYTES:
            raise DomainError("Configuration exceeds 16 KiB.")
        return json.loads(encoded)
    except (ValueError, TypeError, RecursionError, UnicodeError) as error:
        raise DomainError(
            "Configuration must be a finite JSON object of at most 16 KiB."
        ) from error


def _validate_name_enabled(name, enabled):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= MAX_NAME_LENGTH:
        raise DomainError("Name must contain 1–128 characters after trimming.")
    if type(enabled) is not bool:
        raise DomainError("Enabled must be a boolean.")
    return name.strip()


def _validated_fields(name, kind, configuration, enabled, kinds):
    name = _validate_name_enabled(name, enabled)
    if not isinstance(kind, str) or kind not in kinds:
        raise DomainError("Unsupported kind.")
    return dict(
        name=name,
        kind=kind,
        configuration=validate_configuration(configuration),
        enabled=enabled,
    )


def get_source(session, source_id):
    source = session.get(Source, source_id)
    if source is None:
        raise DomainError("Source does not exist.")
    return source


def get_widget(session, widget_id):
    widget = session.get(Widget, widget_id)
    if widget is None:
        raise DomainError("Widget does not exist.")
    return widget


def list_sources(session):
    return session.scalars(select(Source).order_by(Source.id)).all()


def list_widgets(session):
    return session.scalars(select(Widget).order_by(Widget.id)).all()


def _validate_source_id(session, source_id):
    if source_id is not None:
        if type(source_id) is not int or source_id < 1:
            raise DomainError("Source identity must be a positive integer or None.")
        get_source(session, source_id)


def create_source(session, *, name, kind, configuration, enabled=True):
    source = Source(
        **_validated_fields(name, kind, configuration, enabled, SOURCE_KINDS)
    )
    session.add(source)
    session.flush()
    return source


def update_source(session, source_id, *, name, kind, configuration, enabled):
    fields = _validated_fields(name, kind, configuration, enabled, SOURCE_KINDS)
    source = get_source(session, source_id)
    authority_changed = source.kind != kind or any(
        source.configuration.get(key) != fields["configuration"].get(key)
        for key in ("path", "recursive")
    )
    if authority_changed or (source.enabled and not enabled):
        supersede_catalog(session, source_id, invalidate=authority_changed)
    for key, value in fields.items():
        setattr(source, key, value)
    session.flush()
    return source


def create_widget(session, *, name, kind, configuration, enabled=True, source_id=None):
    fields = _validated_fields(name, kind, configuration, enabled, WIDGET_KINDS)
    _validate_source_id(session, source_id)
    widget = Widget(**fields, source_id=source_id)
    session.add(widget)
    session.flush()
    return widget


def update_widget(session, widget_id, *, name, kind, configuration, enabled, source_id):
    fields = _validated_fields(name, kind, configuration, enabled, WIDGET_KINDS)
    _validate_source_id(session, source_id)
    widget = get_widget(session, widget_id)
    for key, value in fields.items():
        setattr(widget, key, value)
    widget.source_id = source_id
    session.flush()
    return widget


def remove_source(session, source_id):
    source = get_source(session, source_id)
    if (
        session.scalar(select(Widget.id).where(Widget.source_id == source_id).limit(1))
        is not None
    ):
        raise DomainError(
            "Source is referenced by a Widget; reassign or remove it first."
        )
    session.delete(source)
    session.flush()


def remove_widget(session, widget_id):
    widget = get_widget(session, widget_id)
    if (
        session.scalar(
            select(ScenePlacement.id)
            .where(ScenePlacement.widget_id == widget_id)
            .limit(1)
        )
        is not None
    ):
        raise DomainError(
            "Widget is referenced by a Scene; remove its placements first."
        )
    session.delete(widget)
    session.flush()


SCENE_LAYOUTS = {
    "single": ("main",),
    "split_vertical": ("left", "right"),
    "split_horizontal": ("top", "bottom"),
}


class Scene(Base):
    __tablename__ = "scene"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(MAX_NAME_LENGTH))
    layout: Mapped[str] = mapped_column(String(64))
    duration_seconds: Mapped[int | None]
    enabled: Mapped[bool] = mapped_column(Boolean)


class ScenePlacement(Base):
    __tablename__ = "scene_placement"
    __table_args__ = (
        UniqueConstraint("scene_id", "position", name="uq_scene_placement_position"),
        UniqueConstraint("scene_id", "region", name="uq_scene_placement_region"),
        UniqueConstraint("scene_id", "widget_id", name="uq_scene_placement_widget"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scene_id: Mapped[int] = mapped_column(ForeignKey("scene.id", ondelete="CASCADE"))
    widget_id: Mapped[int] = mapped_column(
        ForeignKey("widget.id", ondelete="RESTRICT"), index=True
    )
    position: Mapped[int]
    region: Mapped[str] = mapped_column(String(64))


def get_scene(session, scene_id):
    scene = session.get(Scene, scene_id)
    if scene is None:
        raise DomainError("Scene does not exist.")
    return scene


def list_scenes(session):
    return session.scalars(select(Scene).order_by(Scene.id)).all()


def list_scene_placements(session, scene_id):
    get_scene(session, scene_id)
    return session.scalars(
        select(ScenePlacement)
        .where(ScenePlacement.scene_id == scene_id)
        .order_by(ScenePlacement.position)
    ).all()


def _validated_scene(session, name, layout, placements, duration_seconds, enabled):
    name = _validate_name_enabled(name, enabled)
    if duration_seconds is not None and (
        type(duration_seconds) is not int or not 1 <= duration_seconds <= 86400
    ):
        raise DomainError(
            "Duration must be None or an integer from 1 to 86400 seconds."
        )
    if not isinstance(layout, str) or layout not in SCENE_LAYOUTS:
        raise DomainError("Unsupported Scene layout.")
    regions = SCENE_LAYOUTS[layout]
    if not isinstance(placements, (list, tuple)) or len(placements) != len(regions):
        raise DomainError("Supply the complete placement set for the layout.")
    by_region = {}
    for placement in placements:
        if not isinstance(placement, (list, tuple)) or len(placement) != 2:
            raise DomainError("Each placement must be a region/Widget identity pair.")
        region, widget_id = placement
        if not isinstance(region, str) or region not in regions or region in by_region:
            raise DomainError("Each required region must appear exactly once.")
        if type(widget_id) is not int or widget_id < 1:
            raise DomainError("Widget identity must be a positive integer.")
        if widget_id in by_region.values():
            raise DomainError("A Widget may appear only once within a Scene.")
        get_widget(session, widget_id)
        by_region[region] = widget_id
    rows = [
        ScenePlacement(position=position, region=region, widget_id=by_region[region])
        for position, region in enumerate(regions)
    ]
    return dict(
        name=name, layout=layout, duration_seconds=duration_seconds, enabled=enabled
    ), rows


def create_scene(
    session, *, name, layout, placements, duration_seconds=None, enabled=True
):
    """Create with complete [(region, widget_id), ...] input in any region order."""
    fields, rows = _validated_scene(
        session, name, layout, placements, duration_seconds, enabled
    )
    scene = Scene(**fields)
    session.add(scene)
    session.flush()
    for row in rows:
        row.scene_id = scene.id
    session.add_all(rows)
    session.flush()
    return scene


def update_scene(
    session, scene_id, *, name, layout, placements, duration_seconds, enabled
):
    """Replace all fields/placements; validate before mutating even within a caught error."""
    fields, rows = _validated_scene(
        session, name, layout, placements, duration_seconds, enabled
    )
    scene = get_scene(session, scene_id)
    session.execute(delete(ScenePlacement).where(ScenePlacement.scene_id == scene_id))
    for key, value in fields.items():
        setattr(scene, key, value)
    for row in rows:
        row.scene_id = scene_id
    session.add_all(rows)
    session.flush()
    return scene


def remove_scene(session, scene_id):
    scene = get_scene(session, scene_id)
    if (
        session.scalar(
            select(SequenceMembership.id)
            .where(SequenceMembership.scene_id == scene_id)
            .limit(1)
        )
        is not None
    ):
        raise DomainError(
            "Scene is referenced by a Sequence; remove its memberships first."
        )
    session.delete(scene)
    session.flush()


SEQUENCE_MODES = frozenset({"ordered", "shuffle"})


class Sequence(Base):
    __tablename__ = "sequence"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(MAX_NAME_LENGTH))
    mode: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean)


class SequenceMembership(Base):
    __tablename__ = "sequence_membership"
    __table_args__ = (
        UniqueConstraint(
            "sequence_id", "position", name="uq_sequence_membership_position"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sequence_id: Mapped[int] = mapped_column(
        ForeignKey("sequence.id", ondelete="CASCADE")
    )
    scene_id: Mapped[int] = mapped_column(
        ForeignKey("scene.id", ondelete="RESTRICT"), index=True
    )
    position: Mapped[int]
    duration_override_seconds: Mapped[int | None]


def get_sequence(session, sequence_id):
    sequence = session.get(Sequence, sequence_id)
    if sequence is None:
        raise DomainError("Sequence does not exist.")
    return sequence


def list_sequences(session):
    return session.scalars(select(Sequence).order_by(Sequence.id)).all()


def list_sequence_memberships(session, sequence_id):
    get_sequence(session, sequence_id)
    return session.scalars(
        select(SequenceMembership)
        .where(SequenceMembership.sequence_id == sequence_id)
        .order_by(SequenceMembership.position)
    ).all()


def _validated_sequence(session, name, mode, memberships, enabled):
    name = _validate_name_enabled(name, enabled)
    if not isinstance(mode, str) or mode not in SEQUENCE_MODES:
        raise DomainError("Unsupported Sequence mode.")
    if not isinstance(memberships, (list, tuple)) or not memberships:
        raise DomainError("Supply a nonempty complete ordered membership list.")
    rows = []
    for position, occurrence in enumerate(memberships):
        if not isinstance(occurrence, (list, tuple)) or len(occurrence) != 2:
            raise DomainError("Each membership must be a Scene identity/duration pair.")
        scene_id, duration = occurrence
        if type(scene_id) is not int or scene_id < 1:
            raise DomainError("Scene identity must be a positive integer.")
        get_scene(session, scene_id)
        if duration is not None and (
            type(duration) is not int or not 1 <= duration <= 86400
        ):
            raise DomainError(
                "Duration override must be None or an integer from 1 to 86400 seconds."
            )
        rows.append(
            SequenceMembership(
                scene_id=scene_id, position=position, duration_override_seconds=duration
            )
        )
    return dict(name=name, mode=mode, enabled=enabled), rows


def create_sequence(session, *, name, mode, memberships, enabled=True):
    """Create configured occurrences from ordered [(scene_id, duration), ...]."""
    fields, rows = _validated_sequence(session, name, mode, memberships, enabled)
    sequence = Sequence(**fields)
    session.add(sequence)
    session.flush()
    for row in rows:
        row.sequence_id = sequence.id
    session.add_all(rows)
    session.flush()
    return sequence


def update_sequence(session, sequence_id, *, name, mode, memberships, enabled):
    """Replace all configuration atomically; membership identities may change."""
    fields, rows = _validated_sequence(session, name, mode, memberships, enabled)
    sequence = get_sequence(session, sequence_id)
    session.execute(
        delete(SequenceMembership).where(SequenceMembership.sequence_id == sequence_id)
    )
    for key, value in fields.items():
        setattr(sequence, key, value)
    for row in rows:
        row.sequence_id = sequence_id
    session.add_all(rows)
    session.flush()
    return sequence


def remove_sequence(session, sequence_id):
    session.delete(get_sequence(session, sequence_id))
    session.flush()
