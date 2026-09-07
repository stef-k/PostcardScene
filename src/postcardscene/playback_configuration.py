"""Detached V0 configuration only; no content selection or playback state.

Each call reads current configuration in one transaction. Call again at display
step boundaries. A restart begins a fresh transient ordered/shuffle epoch.
"""

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select

from postcardscene.domain import Scene, ScenePlacement, Sequence, SequenceMembership
from postcardscene.persistence import Database
from postcardscene.settings import PlaybackSettings, read_playback_settings


class Eligibility(StrEnum):
    READY = "ready"
    IDLE = "idle"
    SEQUENCE_MISSING = "sequence_missing"
    SEQUENCE_DISABLED = "sequence_disabled"
    SEQUENCE_DAMAGED = "sequence_damaged"
    SCENE_MISSING = "scene_missing"
    SCENE_DISABLED = "scene_disabled"
    UNSUPPORTED_LAYOUT = "unsupported_layout"
    OCCURRENCE_DAMAGED = "occurrence_damaged"


@dataclass(frozen=True)
class MembershipSnapshot:
    id: int
    scene_id: int
    position: int
    duration_override_seconds: int | None


@dataclass(frozen=True)
class SequenceSnapshot:
    id: int
    mode: str
    enabled: bool
    memberships: tuple[MembershipSnapshot, ...]


@dataclass(frozen=True)
class SceneSnapshot:
    id: int
    layout: str
    enabled: bool
    duration_seconds: int | None
    widget_id: int | None


@dataclass(frozen=True)
class PlaybackConfiguration:
    settings: PlaybackSettings
    eligibility: Eligibility
    sequence: SequenceSnapshot | None = None
    membership: MembershipSnapshot | None = None
    scene: SceneSnapshot | None = None


def _duration_valid(value):
    return value is None or (type(value) is int and 1 <= value <= 86400)


def _identity_valid(value):
    return type(value) is int and 1 <= value <= 2**63 - 1


def _read_sequence(session, identity):
    row = session.get(Sequence, identity)
    if row is None:
        return Eligibility.SEQUENCE_MISSING, None
    memberships = tuple(
        MembershipSnapshot(m.id, m.scene_id, m.position, m.duration_override_seconds)
        for m in session.scalars(
            select(SequenceMembership)
            .where(SequenceMembership.sequence_id == identity)
            .order_by(SequenceMembership.position)
        )
    )
    snapshot = SequenceSnapshot(row.id, row.mode, row.enabled, memberships)
    if row.mode not in ("ordered", "shuffle") or not memberships:
        return Eligibility.SEQUENCE_DAMAGED, snapshot
    for position, member in enumerate(memberships):
        if (
            member.position != position
            or not _identity_valid(member.id)
            or not _identity_valid(member.scene_id)
            or not _duration_valid(member.duration_override_seconds)
        ):
            return Eligibility.OCCURRENCE_DAMAGED, snapshot
    return (
        Eligibility.READY if row.enabled else Eligibility.SEQUENCE_DISABLED
    ), snapshot


def _read_scene(session, identity):
    row = session.get(Scene, identity)
    if row is None:
        return Eligibility.SCENE_MISSING, None
    placements = session.scalars(
        select(ScenePlacement).where(ScenePlacement.scene_id == identity).limit(2)
    ).all()
    canonical = (
        len(placements) == 1
        and placements[0].position == 0
        and placements[0].region == "main"
        and _identity_valid(placements[0].widget_id)
    )
    snapshot = SceneSnapshot(
        row.id,
        row.layout,
        row.enabled,
        row.duration_seconds,
        placements[0].widget_id if row.layout == "single" and canonical else None,
    )
    if row.layout in ("split_vertical", "split_horizontal"):
        return Eligibility.UNSUPPORTED_LAYOUT, snapshot
    if (
        row.layout != "single"
        or not canonical
        or not _duration_valid(row.duration_seconds)
    ):
        return Eligibility.OCCURRENCE_DAMAGED, snapshot
    return (Eligibility.READY if row.enabled else Eligibility.SCENE_DISABLED), snapshot


def resolve_active_sequence(
    database: Database, *, membership_id: int | None = None
) -> PlaybackConfiguration:
    """Read the active Sequence and optionally one of its Scene occurrences.

    Unknown/stale/foreign membership identities are damaged occurrences, never
    fallback selections. Storage exceptions propagate separately from eligibility.
    Widget/Source semantics belong to the content-specific resolution seams.
    """
    with database.transaction() as session:
        settings = read_playback_settings(session)
        identity = settings.active_sequence_id
        if identity is None:
            return PlaybackConfiguration(settings, Eligibility.IDLE)
        if not _identity_valid(identity):
            return PlaybackConfiguration(settings, Eligibility.SEQUENCE_DAMAGED)
        status, sequence = _read_sequence(session, identity)
        if status != Eligibility.READY or membership_id is None:
            return PlaybackConfiguration(settings, status, sequence)
        member = (
            next((m for m in sequence.memberships if m.id == membership_id), None)
            if _identity_valid(membership_id)
            else None
        )
        if member is None:
            return PlaybackConfiguration(
                settings, Eligibility.OCCURRENCE_DAMAGED, sequence
            )
        status, scene = _read_scene(session, member.scene_id)
        return PlaybackConfiguration(settings, status, sequence, member, scene)
