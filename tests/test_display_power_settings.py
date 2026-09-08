from dataclasses import FrozenInstanceError

import pytest
from sqlalchemy.exc import IntegrityError

from postcardscene.persistence import Database, DatabaseError
from postcardscene.schema import upgrade_database
from postcardscene.settings import (
    DisplayPowerSettings,
    get_display_power_settings,
    set_display_power_settings,
)


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "state.sqlite3")
    upgrade_database(database.path)
    yield database
    database.engine.dispose()


def test_defaults_detached_snapshot_and_complete_updates(database):
    original = get_display_power_settings(database)
    assert original == DisplayPowerSettings("auto", 5, 1800)
    with pytest.raises(FrozenInstanceError):
        original.display_power_backend = "cec"
    for backend, wake, dwell in (
        ("cec", 0, 300),
        ("ddc", 30, 14400),
        ("signal", 5, 1800),
        ("auto", 7, 600),
    ):
        set_display_power_settings(database, backend, wake, dwell)
        reopened = Database(database.path)
        try:
            assert get_display_power_settings(reopened) == DisplayPowerSettings(
                backend, wake, dwell
            )
        finally:
            reopened.engine.dispose()
    assert original == DisplayPowerSettings("auto", 5, 1800)


@pytest.mark.parametrize(
    "backend", ["disabled", "CEC", "plugin", "", None, True, 1, []]
)
def test_invalid_backend_preserves_policy(database, backend):
    with pytest.raises(ValueError):
        set_display_power_settings(database, backend, 0, 300)
    assert get_display_power_settings(database) == DisplayPowerSettings("auto", 5, 1800)


@pytest.mark.parametrize(
    "wake,dwell",
    [
        (True, 300),
        (False, 300),
        (-1, 300),
        (31, 300),
        (1.5, 300),
        ("5", 300),
        (0, True),
        (0, False),
        (0, 299),
        (0, 14401),
        (0, 300.5),
        (0, "300"),
        (None, 300),
        (0, None),
    ],
)
def test_invalid_bounds_preserve_complete_policy(database, wake, dwell):
    set_display_power_settings(database, "ddc", 7, 900)
    with pytest.raises(ValueError):
        set_display_power_settings(database, "cec", wake, dwell)
    assert get_display_power_settings(database) == DisplayPowerSettings("ddc", 7, 900)


@pytest.mark.parametrize(
    "assignment",
    [
        "display_power_backend='disabled'",
        "display_power_backend=NULL",
        "display_wake_delay_seconds=-1",
        "display_wake_delay_seconds=31",
        "display_wake_delay_seconds=0.5",
        "display_wake_delay_seconds=NULL",
        "maximum_static_dwell_seconds=299",
        "maximum_static_dwell_seconds=14401",
        "maximum_static_dwell_seconds=300.5",
        "maximum_static_dwell_seconds=NULL",
    ],
)
def test_database_constraints(database, assignment):
    with pytest.raises(IntegrityError):
        with database.engine.begin() as connection:
            connection.exec_driver_sql(f"UPDATE application_settings SET {assignment}")
    assert get_display_power_settings(database) == DisplayPowerSettings("auto", 5, 1800)


@pytest.mark.parametrize(
    "assignment",
    [
        "display_power_backend='disabled'",
        "display_wake_delay_seconds=0.5",
        "maximum_static_dwell_seconds=0",
    ],
)
def test_damaged_policy_is_not_defaulted_or_overwritten(database, assignment):
    with database.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.exec_driver_sql(f"UPDATE application_settings SET {assignment}")
    with pytest.raises(DatabaseError, match="damaged"):
        get_display_power_settings(database)
    with pytest.raises(DatabaseError, match="damaged"):
        set_display_power_settings(database, "auto", 5, 1800)


def test_missing_policy_fails_closed(database):
    with database.engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM application_settings")
    with pytest.raises(DatabaseError, match="missing"):
        get_display_power_settings(database)
    with pytest.raises(DatabaseError, match="missing"):
        set_display_power_settings(database, "auto", 5, 1800)
