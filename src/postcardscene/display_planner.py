"""Serialized, transient display-step planning, without execution or clocks."""

import random
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy.exc import SQLAlchemyError

from postcardscene.domain import Source, Widget
from postcardscene.image_selection import IMAGE_WIDGET_KINDS, build_image_frame
from postcardscene.media_stream import resolve_candidate, select_candidate
from postcardscene.persistence import DatabaseError
from postcardscene.playback_configuration import Eligibility, resolve_active_sequence
from postcardscene.web_selection import resolve_web_target


class PlanStatus(StrEnum):
    READY = "ready"
    INELIGIBLE = "ineligible"
    HISTORY_BOUNDARY = "history_boundary"
    FAILURE = "failure"


@dataclass(frozen=True)
class DisplayStep:
    """Revalidation hint only: no paths to files, URLs or renderer authority."""

    sequence_id: int
    membership_id: int
    scene_id: int
    widget_id: int
    kind: str
    media: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class PlanResult:
    status: PlanStatus
    step: DisplayStep | None = None


@dataclass
class _Stream:
    kind: str
    after: tuple[int, str] | None = None
    pending: tuple[int, str] | None = None
    recent: deque = field(default_factory=lambda: deque(maxlen=32))

    def consume(self, candidate):
        self.after = candidate.identity
        self.recent.append(candidate.identity)


class DisplayPlanner:
    """One owner calls next/previous serially. Construction starts a fresh epoch.

    Each successful new plan is recorded as a display step; the future execution
    owner decides when to request progression. One call attempts one occurrence,
    leaving retry/backoff and actual display success policy to that owner.
    History replay never advances candidate streams. Returned identities must be
    revalidated again at execution; planning does not pin configuration or files.
    """

    def __init__(self, database, *, rng=None):
        self.database = database
        self.rng = rng if rng is not None else random.Random()
        self._epoch = None
        self._reset()

    def _reset(self):
        self._streams = {}
        self._history = []
        self._cursor = -1
        self._cycle = deque()
        self._last_member = None

    @property
    def history(self):
        return tuple(self._history)

    @staticmethod
    def _epoch_key(configuration):
        sequence = configuration.sequence
        if sequence is None or not sequence.enabled:
            return None
        return (
            sequence.id,
            sequence.mode,
            tuple((m.id, m.scene_id) for m in sequence.memberships),
        )

    def _refresh(self):
        configuration = resolve_active_sequence(self.database)
        key = self._epoch_key(configuration)
        if key != self._epoch:
            self._reset()
            self._epoch = key
        return configuration

    def next(self):
        """Next and automatic progression share forward-history semantics."""
        return self._request(1)

    def previous(self):
        return self._request(-1)

    def _request(self, direction):
        try:
            configuration = self._refresh()
            if configuration.eligibility != Eligibility.READY:
                return PlanResult(PlanStatus.INELIGIBLE)
            replay = self._replay(direction)
            if replay is not None:
                return replay
            if self._epoch_key(configuration) != self._epoch:
                return PlanResult(PlanStatus.INELIGIBLE)
            if direction < 0:
                return PlanResult(PlanStatus.HISTORY_BOUNDARY)
            member = self._next_member(configuration.sequence)
            step = self._plan(member)
            if step is None:
                return PlanResult(PlanStatus.INELIGIBLE)
            self._history.append(step)
            self._history = self._history[-32:]
            self._cursor = len(self._history) - 1
            return PlanResult(PlanStatus.READY, step)
        except (DatabaseError, SQLAlchemyError, ValueError):
            # Never expose DB values, URLs or raw exception details in status.
            return PlanResult(PlanStatus.FAILURE)

    def _next_member(self, sequence):
        if not self._cycle:
            members = [m.id for m in sequence.memberships]
            if sequence.mode == "shuffle":
                self.rng.shuffle(members)
                if len(members) > 1 and members[0] == self._last_member:
                    members[0], members[1] = members[1], members[0]
            self._cycle.extend(members)
        member = self._cycle.popleft()
        self._last_member = member
        return member

    def _composition(self, member):
        config = resolve_active_sequence(self.database, membership_id=member)
        if self._epoch_key(config) != self._epoch:
            # Concurrent replacement: reset now, start the new epoch next call.
            self._epoch = self._epoch_key(config)
            self._reset()
            return None
        if config.eligibility != Eligibility.READY:
            return None
        with self.database.transaction() as session:
            widget = session.get(Widget, config.scene.widget_id)
            if widget is None or not widget.enabled:
                return None
            source = session.get(Source, widget.source_id) if widget.source_id else None
            if source is None or not source.enabled:
                return None
            kind = widget.kind
        if kind not in IMAGE_WIDGET_KINDS | {"video", "web_view"}:
            return None
        return DisplayStep(
            config.sequence.id, member, config.scene.id, config.scene.widget_id, kind
        )

    def _plan(self, member):
        step = self._composition(member)
        if step is None:
            return None
        if step.kind == "web_view":
            resolve_web_target(self.database, step.widget_id)
            return step
        stream = self._streams.get(step.widget_id)
        if stream is None or stream.kind != step.kind:
            stream = _Stream(step.kind)
            self._streams[step.widget_id] = stream
        first = None
        if stream.pending is not None:
            first = resolve_candidate(
                self.database, step.widget_id, step.kind, stream.pending
            )
            stream.pending = None
        if first is None:
            first = self._candidate(step, stream)
        if first is None:
            return None
        selected = (first,)
        if step.kind == "portrait_image_pair":
            # Draw as though first were consumed; commit consumption only after
            # grouping. A rejected lookahead remains owned by this Widget.
            lookahead = self._candidate(step, stream, first)
            if lookahead is not None and lookahead.identity == first.identity:
                lookahead = None
            frame, consumed = build_image_frame(step.kind, "contain", first, lookahead)
            selected = frame.images
            if consumed == 1 and lookahead is not None:
                stream.pending = lookahead.identity
        for candidate in selected:
            stream.consume(candidate)
        return DisplayStep(
            step.sequence_id,
            step.membership_id,
            step.scene_id,
            step.widget_id,
            step.kind,
            tuple(c.identity for c in selected),
        )

    def _candidate(self, step, stream, first=None):
        recent = tuple(stream.recent)
        after = stream.after
        if first is not None:
            after = first.identity
            recent = (*recent, first.identity)[-32:]
        shuffled = self._epoch[1] == "shuffle"
        fraction = self.rng.random() if shuffled else None
        return select_candidate(
            self.database,
            step.widget_id,
            step.kind,
            after=after,
            recent=recent,
            random_fraction=fraction,
        )

    def _valid_history(self, step):
        current = self._composition(step.membership_id)
        if current is None or (
            current.sequence_id,
            current.scene_id,
            current.widget_id,
            current.kind,
        ) != (step.sequence_id, step.scene_id, step.widget_id, step.kind):
            return False
        if step.kind == "web_view":
            resolve_web_target(self.database, step.widget_id)
            return True
        candidates = [
            resolve_candidate(self.database, step.widget_id, step.kind, identity)
            for identity in step.media
        ]
        if not all(candidates):
            return False
        if step.kind == "portrait_image_pair" and len(candidates) == 2:
            _, consumed = build_image_frame(step.kind, "contain", *candidates)
            return consumed == 2
        return True

    def _replay(self, direction):
        index = self._cursor + direction
        while 0 <= index < len(self._history):
            step = self._history[index]
            try:
                valid = self._valid_history(step)
            except ValueError:
                valid = False
            if valid:
                self._cursor = index
                return PlanResult(PlanStatus.READY, step)
            index += direction
        return None
