"""Audit the four-part configuration graph without initializing Flask or runtime."""

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from postcardscene import domain as d
from postcardscene.persistence import Database
from postcardscene.schema import upgrade_database


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "lifecycle.sqlite3")
    upgrade_database(database.path)
    yield database
    database.engine.dispose()


def test_complete_graph_lifecycle(database):
    source_fields = dict(name="Photos", kind="local_directory", configuration={})
    with database.transaction() as session:
        source_id = d.create_source(session, **source_fields).id
        widget_fields = dict(
            name="Image",
            kind="image",
            configuration={"fit": "contain"},
            source_id=source_id,
        )
        widget_id = d.create_widget(session, **widget_fields).id
        other_widget_id = d.create_widget(
            session, name="Web", kind="web_view", configuration={}
        ).id
        scene_fields = dict(
            name="Split",
            layout="split_vertical",
            duration_seconds=30,
            placements=[("right", other_widget_id), ("left", widget_id)],
        )
        scene_id = d.create_scene(session, **scene_fields).id
        sequence_fields = dict(
            name="Loop", mode="shuffle", memberships=[(scene_id, None), (scene_id, 5)]
        )
        sequence_id = d.create_sequence(session, **sequence_fields).id
        placement_ids = [p.id for p in d.list_scene_placements(session, scene_id)]
        membership_ids = [
            m.id for m in d.list_sequence_memberships(session, sequence_id)
        ]

    # Reconstruct the application-owned database handle, not just the session.
    database.engine.dispose()
    reconstructed = Database(database.path)
    try:
        _assert_graph(
            reconstructed,
            source_id,
            widget_id,
            other_widget_id,
            scene_id,
            sequence_id,
            [True] * 4,
        )
        with reconstructed.transaction() as session:
            assert [
                p.id for p in d.list_scene_placements(session, scene_id)
            ] == placement_ids
            assert [
                m.id for m in d.list_sequence_memberships(session, sequence_id)
            ] == membership_ids
            # Both errors are caught inside a transaction that will commit.
            with pytest.raises(d.DomainError, match="Widget does not exist"):
                d.update_scene(
                    session,
                    scene_id,
                    **{
                        **scene_fields,
                        "name": "Invalid",
                        "layout": "split_horizontal",
                        "placements": [
                            ("top", widget_id),
                            ("bottom", other_widget_id + 1),
                        ],
                    },
                    enabled=False,
                )
            with pytest.raises(d.DomainError, match="Scene does not exist"):
                d.update_sequence(
                    session,
                    sequence_id,
                    **{
                        **sequence_fields,
                        "name": "Invalid",
                        "mode": "ordered",
                        "memberships": [(scene_id, 10), (scene_id + 1, None)],
                    },
                    enabled=False,
                )
        _assert_graph(
            reconstructed,
            source_id,
            widget_id,
            other_widget_id,
            scene_id,
            sequence_id,
            [True] * 4,
        )
        with reconstructed.transaction() as session:
            assert [
                p.id for p in d.list_scene_placements(session, scene_id)
            ] == placement_ids
            assert [
                m.id for m in d.list_sequence_memberships(session, sequence_id)
            ] == membership_ids

        # Updating containers also proves disabled children remain valid references.
        enabled = [True] * 4
        for index, (update, identity, fields) in enumerate(
            [
                (d.update_source, source_id, source_fields),
                (d.update_widget, widget_id, widget_fields),
                (d.update_scene, scene_id, scene_fields),
                (d.update_sequence, sequence_id, sequence_fields),
            ]
        ):
            with reconstructed.transaction() as session:
                update(session, identity, **fields, enabled=False)
            enabled[index] = False
            _assert_graph(
                reconstructed,
                source_id,
                widget_id,
                other_widget_id,
                scene_id,
                sequence_id,
                enabled,
            )

        for model, remove, identity in [
            (d.Source, d.remove_source, source_id),
            (d.Widget, d.remove_widget, widget_id),
            (d.Scene, d.remove_scene, scene_id),
        ]:
            with reconstructed.transaction() as session:
                with pytest.raises(d.DomainError, match="referenced"):
                    remove(session, identity)
            with pytest.raises(IntegrityError):
                with reconstructed.transaction() as session:
                    session.execute(delete(model).where(model.id == identity))
        _assert_graph(
            reconstructed,
            source_id,
            widget_id,
            other_widget_id,
            scene_id,
            sequence_id,
            enabled,
        )

        with reconstructed.transaction() as session:
            d.remove_sequence(session, sequence_id)
        with reconstructed.transaction() as session:
            assert d.list_sequences(session) == []
            assert session.scalars(select(d.SequenceMembership)).all() == []
            assert len(d.list_scene_placements(session, scene_id)) == 2
            d.remove_scene(session, scene_id)
        with reconstructed.transaction() as session:
            assert d.list_scenes(session) == []
            assert session.scalars(select(d.ScenePlacement)).all() == []
            assert [w.id for w in d.list_widgets(session)] == [
                widget_id,
                other_widget_id,
            ]
            d.remove_widget(session, widget_id)
        with reconstructed.transaction() as session:
            assert d.get_source(session, source_id).enabled is False
            d.remove_source(session, source_id)
        with reconstructed.transaction() as session:
            assert d.list_sources(session) == []
            assert [w.id for w in d.list_widgets(session)] == [other_widget_id]
            assert d.get_widget(session, other_widget_id).source_id is None
        reconstructed.check()
    finally:
        reconstructed.engine.dispose()


def _assert_graph(
    database, source_id, widget_id, other_widget_id, scene_id, sequence_id, enabled
):
    with database.transaction() as session:
        source = d.get_source(session, source_id)
        widget = d.get_widget(session, widget_id)
        scene = d.get_scene(session, scene_id)
        sequence = d.get_sequence(session, sequence_id)
        assert [
            source.enabled,
            widget.enabled,
            scene.enabled,
            sequence.enabled,
        ] == enabled
        assert (source.name, source.kind, source.configuration) == (
            "Photos",
            "local_directory",
            {},
        )
        assert (widget.name, widget.kind, widget.configuration, widget.source_id) == (
            "Image",
            "image",
            {"fit": "contain"},
            source_id,
        )
        assert d.get_widget(session, other_widget_id).enabled is True
        assert (scene.name, scene.layout, scene.duration_seconds) == (
            "Split",
            "split_vertical",
            30,
        )
        assert (sequence.name, sequence.mode) == ("Loop", "shuffle")
        assert [
            (p.scene_id, p.position, p.region, p.widget_id)
            for p in d.list_scene_placements(session, scene_id)
        ] == [
            (scene_id, 0, "left", widget_id),
            (scene_id, 1, "right", other_widget_id),
        ]
        assert [
            (m.sequence_id, m.position, m.scene_id, m.duration_override_seconds)
            for m in d.list_sequence_memberships(session, sequence_id)
        ] == [
            (sequence_id, 0, scene_id, None),
            (sequence_id, 1, scene_id, 5),
        ]
