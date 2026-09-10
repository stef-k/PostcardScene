"""Persist singleton backup policy and bounded advisory history."""

import sqlalchemy as sa
from alembic import op

revision = "0012_backup_policy"
down_revision = "0011_display_power_settings"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "backup_policy",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("destination_path", sa.String(4096), nullable=True),
        sa.Column("local_hour", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("retention_count", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("last_attempt_ns", sa.Integer(), nullable=True),
        sa.Column("last_success_ns", sa.Integer(), nullable=True),
        sa.Column("last_success_local_date", sa.String(10), nullable=True),
        sa.Column("last_archive_filename", sa.String(255), nullable=True),
        sa.Column("last_result", sa.String(24), nullable=False, server_default="never"),
        sa.CheckConstraint(
            "last_archive_filename IS NULL OR (length(last_archive_filename) BETWEEN 1 AND 255 AND instr(last_archive_filename, '/') = 0 AND instr(last_archive_filename, char(0)) = 0)",
            name=op.f("ck_backup_policy_archive_basename"),
        ),
        sa.CheckConstraint(
            "destination_path IS NULL OR (length(destination_path) BETWEEN 1 AND 4096 AND substr(destination_path, 1, 1) = '/' AND instr(destination_path, char(0)) = 0)",
            name=op.f("ck_backup_policy_destination_path"),
        ),
        sa.CheckConstraint(
            "enabled = 0 OR destination_path IS NOT NULL",
            name=op.f("ck_backup_policy_destination_required"),
        ),
        sa.CheckConstraint(
            "enabled IN (0, 1)", name=op.f("ck_backup_policy_enabled_bool")
        ),
        sa.CheckConstraint(
            "last_attempt_ns IS NULL OR (typeof(last_attempt_ns) = 'integer' AND last_attempt_ns >= 0)",
            name=op.f("ck_backup_policy_last_attempt_ns"),
        ),
        sa.CheckConstraint(
            "last_result IN ('never', 'ready', 'failed', 'ready_retention_degraded')",
            name=op.f("ck_backup_policy_last_result"),
        ),
        sa.CheckConstraint(
            "last_success_ns IS NULL OR (typeof(last_success_ns) = 'integer' AND last_success_ns >= 0)",
            name=op.f("ck_backup_policy_last_success_ns"),
        ),
        sa.CheckConstraint(
            "typeof(local_hour) = 'integer' AND local_hour BETWEEN 0 AND 23",
            name=op.f("ck_backup_policy_local_hour"),
        ),
        sa.CheckConstraint(
            "typeof(retention_count) = 'integer' AND retention_count BETWEEN 1 AND 30",
            name=op.f("ck_backup_policy_retention_count"),
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_backup_policy_singleton")),
        sa.CheckConstraint(
            "last_success_local_date IS NULL OR (length(last_success_local_date) = 10 AND last_success_local_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')",
            name=op.f("ck_backup_policy_success_date"),
        ),
        sa.CheckConstraint(
            "(last_success_ns IS NULL AND last_success_local_date IS NULL AND last_archive_filename IS NULL) OR (last_success_ns IS NOT NULL AND last_success_local_date IS NOT NULL AND last_archive_filename IS NOT NULL)",
            name=op.f("ck_backup_policy_success_identity"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backup_policy")),
    )
    op.execute("INSERT INTO backup_policy (id) VALUES (1)")


def downgrade():
    raise NotImplementedError("Downgrade is not supported.")
