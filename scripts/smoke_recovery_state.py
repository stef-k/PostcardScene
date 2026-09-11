"""Private installed recovery evidence; stdout is captured, never logged."""

import json
import secrets
import sqlite3
import sys
from pathlib import Path

DATABASE = Path("/var/lib/postcardscene/postcardscene.sqlite3")
DURABLE = (
    "administrator",
    "application_settings",
    "source",
    "widget",
    "scene",
    "scene_placement",
    "sequence",
    "sequence_membership",
    "operating_window",
    "backup_policy",
)


def seed():
    from postcardscene.accounts import Administrator, set_password
    from postcardscene.backup_policy import record_backup_attempt, replace_backup_policy
    from postcardscene.catalog import MediaCatalogState, MediaItem
    from postcardscene.domain import (
        create_scene,
        create_sequence,
        create_source,
        create_widget,
    )
    from postcardscene.operating_schedule import (
        WeeklyWindow,
        replace_operating_schedule,
    )
    from postcardscene.persistence import Database
    from postcardscene.settings import set_active_sequence, set_timezone

    db = Database()
    with db.transaction() as session:
        session.get(Administrator, 1).username = "recovery-original"
    set_password(db, "recovery-original", secrets.token_urlsafe(32))
    set_timezone(db, "Europe/Athens")
    replace_operating_schedule(db, True, (WeeklyWindow(2, 321, 987),))
    replace_backup_policy(db, False, "/unavailable/recovery-policy", 17, 4)
    record_backup_attempt(db, 1234567890000000000)
    with db.transaction() as session:
        for index in range(2):
            source = create_source(
                session,
                name=f"Recovery source {index}",
                kind="local_directory",
                configuration={"path": f"/private/recovery-{index}", "recursive": True},
                enabled=False,
            )
            session.add(
                MediaCatalogState(
                    source_id=source.id,
                    scan_generation=7,
                    completed_generation=7,
                    requested_generation=3,
                    handled_request_generation=3,
                    last_result="ready",
                    last_attempt_ns=123456789,
                    last_success_ns=123456789,
                )
            )
            session.add(
                MediaItem(
                    source_id=source.id,
                    relative_path="original.jpg",
                    media_type="image",
                    size_bytes=1234,
                    mtime_ns=123456789,
                    seen_generation=7,
                    display_width=800,
                    display_height=600,
                    orientation="landscape",
                    metadata_status="ready",
                )
            )
        widget = create_widget(
            session,
            name="Recovery widget",
            kind="image",
            configuration={},
            source_id=source.id,
        )
        scene = create_scene(
            session,
            name="Recovery scene",
            layout="single",
            placements=[("main", widget.id)],
            duration_seconds=37,
        )
        sequence = create_sequence(
            session,
            name="Recovery sequence",
            mode="ordered",
            memberships=[(scene.id, 43)],
        )
        sequence_id = sequence.id
    set_active_sequence(db, sequence_id)
    db.engine.dispose()


def snapshot(invalidated=False):
    with sqlite3.connect(DATABASE) as db:
        # Fixed product table names only; compare every persisted column, including
        # hashes/session identities, privately in the parent process.
        state = {
            table: db.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()
            for table in DURABLE
        }
        if invalidated:
            assert db.execute("SELECT count(*) FROM media_item").fetchone() == (0,)
            rows = db.execute(
                "SELECT scan_generation, completed_generation, "
                "requested_generation, handled_request_generation, last_result, "
                "last_attempt_ns, last_success_ns FROM media_catalog_state"
            ).fetchall()
            assert rows == [(8, 8, 3, 3, "never_scanned", None, None)] * 2
        print(json.dumps(state, sort_keys=True))


if __name__ == "__main__":
    if sys.argv[1] == "seed":
        seed()
    snapshot(sys.argv[1] == "restored")
