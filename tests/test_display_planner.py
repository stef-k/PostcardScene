"""Planner contracts at the real SQLite selection/composition seam."""

import random

import pytest
from sqlalchemy import delete, event

from postcardscene import domain as d
from postcardscene.catalog import MediaItem
from postcardscene.display_planner import DisplayPlanner, PlanStatus
from postcardscene.media_stream import select_candidate
from postcardscene.settings import set_active_sequence


@pytest.fixture
def playback(catalog):
    db, source, _, _ = catalog
    with db.transaction() as session:
        widget = d.create_widget(
            session, name="Photos", kind="image", configuration={}, source_id=source
        )
        scene = d.create_scene(
            session, name="Scene", layout="single", placements=[("main", widget.id)]
        )
        sequence = d.create_sequence(
            session,
            name="Sequence",
            mode="ordered",
            memberships=[(scene.id, None), (scene.id, None)],
        )
        ids = source, widget.id, scene.id, sequence.id
    set_active_sequence(db, ids[3])
    return db, ids


def populate(playback, paths, *, kind="image", orientations=None):
    db, (source, widget, _, _) = playback
    with db.transaction() as session:
        d.get_widget(session, widget).kind = kind
        session.add_all(
            [
                MediaItem(
                    source_id=source,
                    relative_path=path,
                    media_type="video" if kind == "video" else "image",
                    size_bytes=10,
                    mtime_ns=20,
                    seen_generation=1,
                    display_width=10,
                    display_height=20,
                    orientation=orientations[i] if orientations else "portrait",
                    metadata_status="ready",
                )
                for i, path in enumerate(paths)
            ]
        )


def step(planner):
    result = planner.next()
    assert result.status == PlanStatus.READY
    return result.step


@pytest.mark.parametrize("kind", ["image", "video"])
def test_canonical_order_shared_occurrences_history_and_restart(playback, kind):
    db, ids = playback
    populate(playback, ["z", "a", "m"], kind=kind)
    planner = DisplayPlanner(db)
    planned = [step(planner) for _ in range(4)]
    assert [s.media[0][1] for s in planned] == ["a", "m", "z", "a"]
    assert [s.membership_id for s in planned] == [1, 2, 1, 2]
    assert planner.previous().step == planned[2]
    assert planner.previous().step == planned[1]
    assert step(planner) == planned[2]
    assert step(planner) == planned[3]
    assert step(planner).media == ((ids[0], "m"),)
    assert step(DisplayPlanner(db)) == planned[0]


def test_history_bound_and_oldest_boundary(playback):
    db, _ = playback
    populate(playback, [str(i).zfill(3) for i in range(40)])
    planner = DisplayPlanner(db)
    planned = [step(planner) for _ in range(40)]
    assert planner.history == tuple(planned[-32:])
    for expected in reversed(planned[8:-1]):
        assert planner.previous().step == expected
    assert planner.previous().status == PlanStatus.HISTORY_BOUNDARY
    assert step(planner) == planned[9]


def test_shuffle_cycles_recent_window_and_no_transaction_during_rng(playback):
    db, (_, _, _, sequence) = playback
    populate(playback, [str(i).zfill(3) for i in range(80)])
    with db.transaction() as session:
        d.get_sequence(session, sequence).mode = "shuffle"
    checked_out = []
    event.listen(db.engine, "checkout", lambda *args: checked_out.append(1))
    event.listen(db.engine, "checkin", lambda *args: checked_out.pop())

    class CheckedRandom(random.Random):
        def random(self):
            assert not checked_out
            return super().random()

    planner = DisplayPlanner(db, rng=CheckedRandom(7))
    steps = [step(planner) for _ in range(100)]
    for i, current in enumerate(steps):
        assert current.media not in [s.media for s in steps[max(0, i - 32) : i]]
    for i in range(0, 100, 2):
        assert {s.membership_id for s in steps[i : i + 2]} == {1, 2}
        if i:
            assert steps[i].membership_id != steps[i - 1].membership_id
    with db.transaction() as session:
        assert [m.id for m in d.list_sequence_memberships(session, sequence)] == [1, 2]


@pytest.mark.parametrize("count", [1, 2, 3, 32])
def test_small_shuffle_exhausts_before_reuse(playback, count):
    db, (_, _, _, sequence) = playback
    populate(playback, [str(i) for i in range(count)])
    with db.transaction() as session:
        d.get_sequence(session, sequence).mode = "shuffle"
    planner = DisplayPlanner(db, rng=random.Random(9))
    identities = [step(planner).media for _ in range(count * 3)]
    for i in range(len(identities) - count + 1):
        assert len(set(identities[i : i + count])) == count


@pytest.mark.parametrize("mode", ["ordered", "shuffle"])
def test_pair_lookahead_is_retained_and_not_consumed(playback, mode):
    db, (source, _, _, sequence) = playback

    class FirstRank(random.Random):
        def random(self):
            return 0.0

    with db.transaction() as session:
        d.get_sequence(session, sequence).mode = mode
    populate(
        playback,
        ["a", "b", "c", "d"],
        kind="portrait_image_pair",
        orientations=["portrait", "landscape", "portrait", "portrait"],
    )
    planner = DisplayPlanner(db, rng=FirstRank())
    assert step(planner).media == ((source, "a"),)
    assert step(planner).media == ((source, "b"),)
    pair = step(planner)
    assert pair.media == ((source, "c"), (source, "d"))
    assert planner.previous().step.media == ((source, "b"),)
    assert step(planner) == pair
    assert step(planner).media == ((source, "a"),)


def test_invalidated_exact_pair_skipped_without_rebuilding(playback):
    db, _ = playback
    populate(playback, ["a", "b", "c", "d", "e", "f"], kind="portrait_image_pair")
    planner = DisplayPlanner(db)
    first, middle, last = [step(planner) for _ in range(3)]
    with db.transaction() as session:
        session.execute(delete(MediaItem).where(MediaItem.relative_path == "c"))
    assert planner.previous().step == first
    assert step(planner) == last
    assert middle in planner.history


def test_pending_media_is_revalidated(playback):
    db, (source, _, _, _) = playback
    populate(
        playback,
        ["a", "b", "c"],
        kind="portrait_image_pair",
        orientations=["landscape", "landscape", "landscape"],
    )
    planner = DisplayPlanner(db)
    step(planner)
    with db.transaction() as session:
        session.execute(delete(MediaItem).where(MediaItem.relative_path == "b"))
    assert step(planner).media == ((source, "c"),)


@pytest.mark.parametrize("change", ["idle", "disable", "replace", "mode", "active"])
def test_epoch_changes_clear_stream_and_history(playback, change):
    db, (_, _, scene, sequence) = playback
    populate(playback, ["a", "b", "c"])
    planner = DisplayPlanner(db, rng=random.Random(2))
    step(planner)
    step(planner)
    if change == "idle":
        set_active_sequence(db, None)
    else:
        with db.transaction() as session:
            if change == "disable":
                d.get_sequence(session, sequence).enabled = False
            elif change == "replace":
                d.update_sequence(
                    session,
                    sequence,
                    name="Changed",
                    mode="ordered",
                    enabled=True,
                    memberships=[(scene, None)],
                )
            elif change == "mode":
                d.get_sequence(session, sequence).mode = "shuffle"
            else:
                sequence = d.create_sequence(
                    session, name="Other", mode="ordered", memberships=[(scene, None)]
                ).id
        if change == "active":
            set_active_sequence(db, sequence)
    if change in {"idle", "disable"}:
        assert planner.next().status == PlanStatus.INELIGIBLE
        assert planner.history == ()
        with db.transaction() as session:
            d.get_sequence(session, sequence).enabled = True
        set_active_sequence(db, sequence)
    assert planner.previous().status == PlanStatus.HISTORY_BOUNDARY
    new = step(planner)
    assert len(planner.history) == 1
    if change != "mode":
        assert new.media[0][1] == "a"


def test_disabled_occurrence_advances_and_failure_is_typed(playback):
    db, (_, widget, scene, _) = playback
    populate(playback, ["a"])
    planner = DisplayPlanner(db)
    with db.transaction() as session:
        d.get_scene(session, scene).enabled = False
    assert planner.next().status == PlanStatus.INELIGIBLE
    with db.transaction() as session:
        d.get_scene(session, scene).enabled = True
    assert step(planner).membership_id == 2
    with db.transaction() as session:
        d.get_widget(session, widget).configuration = {"fit": "bad"}
    assert planner.next().status == PlanStatus.FAILURE
    with db.engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM application_settings")
    assert planner.next().status == PlanStatus.FAILURE


def test_web_history_resolves_current_target_without_retaining_url(playback):
    db, (source, widget, _, _) = playback
    with db.transaction() as session:
        d.get_widget(session, widget).kind = "web_view"
        row = d.get_source(session, source)
        row.kind = "web_url"
        row.configuration = {"url": "https://example.org/?secret=old"}
    planner = DisplayPlanner(db)
    first = step(planner)
    step(planner)
    assert "https" not in repr(planner.__dict__)
    assert first.media == ()
    with db.transaction() as session:
        d.get_source(session, source).configuration = {"url": "javascript:invalid"}
    assert planner.previous().status == PlanStatus.HISTORY_BOUNDARY
    with db.transaction() as session:
        d.get_source(session, source).configuration = {"url": "https://example.net/new"}
    assert planner.previous().step == first


@pytest.mark.parametrize("kind", ["image", "video"])
def test_large_query_returns_single_candidate_and_uses_canonical_index(playback, kind):
    db, (_, widget, _, _) = playback
    populate(playback, [str(i).zfill(5) for i in reversed(range(2000))], kind=kind)
    statements = []
    event.listen(
        db.engine,
        "before_cursor_execute",
        lambda conn, cursor, sql, params, context, many: statements.append(
            (sql, params)
        ),
    )
    assert select_candidate(db, widget, kind).relative_path == "00000"
    assert (
        select_candidate(db, widget, kind, after=(1, "01999")).relative_path == "00000"
    )
    assert (
        select_candidate(db, widget, kind, random_fraction=0.5).relative_path == "01000"
    )
    selections = [
        (sql, params) for sql, params in statements if "FROM media_item" in sql
    ]
    assert selections
    with db.engine.connect() as connection:
        for sql, params in selections:
            if "count(" in sql:
                continue
            assert "LIMIT" in sql
            assert params[-2] == 1
            plan = connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, params).all()
            assert not any("TEMP B-TREE" in row[3] for row in plan), plan


def test_singleton_pair_does_not_retain_itself_after_library_growth(playback):
    db, (source, _, _, sequence) = playback
    populate(playback, ["a"], kind="portrait_image_pair")
    with db.transaction() as session:
        d.get_sequence(session, sequence).mode = "shuffle"
    planner = DisplayPlanner(db, rng=random.Random(4))
    assert step(planner).media == ((source, "a"),)
    populate(playback, ["b"], kind="portrait_image_pair", orientations=["landscape"])
    assert step(planner).media == ((source, "b"),)


def test_shared_widget_across_scenes_and_invalid_forward_history(playback):
    db, (_, widget, _, sequence) = playback
    populate(playback, ["a", "b", "c", "d"])
    with db.transaction() as session:
        scene = d.create_scene(
            session, name="Second", layout="single", placements=[("main", widget)]
        )
        d.update_sequence(
            session,
            sequence,
            name="Sequence",
            mode="ordered",
            enabled=True,
            memberships=[(1, None), (scene.id, None)],
        )
    planner = DisplayPlanner(db)
    first, middle, last = [step(planner) for _ in range(3)]
    assert [s.media[0][1] for s in (first, middle, last)] == ["a", "b", "c"]
    planner.previous()
    assert planner.previous().step == first
    with db.transaction() as session:
        d.get_scene(session, middle.scene_id).enabled = False
    assert step(planner) == last
    assert planner.next().status == PlanStatus.INELIGIBLE
    assert step(planner).media[0][1] == "d"


def test_held_step_revalidation_preserves_cursor_and_invalidates_deleted_media(
    playback,
):
    db, _ = playback
    populate(playback, ["a", "b"])
    planner = DisplayPlanner(db)
    first = step(planner)
    assert planner.revalidate(first).step == first
    assert planner.history == (first,)
    second = step(planner)
    assert second.media[0][1] == "b"
    with db.transaction() as session:
        session.execute(delete(MediaItem).where(MediaItem.relative_path == "a"))
    assert planner.revalidate(first).status == PlanStatus.INELIGIBLE
    assert planner.history == (first, second)
