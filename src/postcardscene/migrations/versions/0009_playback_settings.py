"""Add explicit active Sequence and fallback dwell without playback state."""

import sqlalchemy as sa
from alembic import op

revision = "0009_playback_settings"
down_revision = "0008_catalog_requests"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("application_settings") as batch:
        batch.add_column(sa.Column("active_sequence_id", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column(
                "default_scene_dwell_seconds",
                sa.Integer(),
                nullable=False,
                server_default="30",
            )
        )
        batch.create_foreign_key(
            "fk_application_settings_active_sequence_id_sequence",
            "sequence",
            ["active_sequence_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_check_constraint(
            "default_scene_dwell_range",
            "typeof(default_scene_dwell_seconds) = 'integer' AND "
            "default_scene_dwell_seconds BETWEEN 1 AND 86400",
        )


def downgrade():
    raise NotImplementedError("Downgrade is not supported.")
