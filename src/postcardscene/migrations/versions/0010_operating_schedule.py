"""Persist weekly active windows and one bounded temporary override."""

import sqlalchemy as sa
from alembic import op

revision = "0010_operating_schedule"
down_revision = "0009_playback_settings"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("application_settings") as batch:
        batch.add_column(
            sa.Column(
                "schedule_enabled", sa.Boolean(), nullable=False, server_default="0"
            )
        )
        batch.add_column(
            sa.Column("schedule_override_active", sa.Boolean(), nullable=True)
        )
        batch.add_column(
            sa.Column("schedule_override_until_utc", sa.Integer(), nullable=True)
        )
        batch.create_check_constraint(
            "schedule_enabled_bool",
            "typeof(schedule_enabled) = 'integer' AND schedule_enabled IN (0, 1)",
        )
        batch.create_check_constraint(
            "schedule_override_bool",
            "schedule_override_active IS NULL OR (typeof(schedule_override_active) = 'integer' AND schedule_override_active IN (0, 1))",
        )
        batch.create_check_constraint(
            "schedule_override_expiry",
            "schedule_override_until_utc IS NULL OR (typeof(schedule_override_until_utc) = 'integer' AND schedule_override_until_utc >= 0)",
        )
        batch.create_check_constraint(
            "schedule_override_pair",
            "(schedule_override_active IS NULL) = (schedule_override_until_utc IS NULL)",
        )
    op.create_table(
        "operating_window",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("start_minute", sa.Integer(), nullable=False),
        sa.Column("end_minute", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "typeof(weekday) = 'integer' AND weekday BETWEEN 0 AND 6",
            name="weekday_range",
        ),
        sa.CheckConstraint(
            "typeof(start_minute) = 'integer' AND start_minute BETWEEN 0 AND 1439",
            name="start_range",
        ),
        sa.CheckConstraint(
            "typeof(end_minute) = 'integer' AND end_minute BETWEEN 1 AND 1440",
            name="end_range",
        ),
        sa.CheckConstraint("start_minute < end_minute", name="day_local_interval"),
    )
    op.execute("""CREATE TRIGGER operating_window_limit BEFORE INSERT ON operating_window
        WHEN (SELECT count(*) FROM operating_window) >= 64
        BEGIN SELECT RAISE(ABORT, 'At most 64 operating windows are allowed'); END""")


def downgrade():
    raise NotImplementedError("Downgrade is not supported.")
