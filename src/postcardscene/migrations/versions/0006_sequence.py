"""Persist Sequence configuration and ordered Scene occurrences."""

import sqlalchemy as sa
from alembic import op

revision = "0006_sequence"
down_revision = "0005_scene"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sequence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("mode", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "sequence_membership",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "sequence_id",
            sa.Integer(),
            sa.ForeignKey("sequence.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "scene_id",
            sa.Integer(),
            sa.ForeignKey("scene.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("duration_override_seconds", sa.Integer(), nullable=True),
        sa.UniqueConstraint(
            "sequence_id", "position", name="uq_sequence_membership_position"
        ),
    )
    op.create_index(
        "ix_sequence_membership_scene_id", "sequence_membership", ["scene_id"]
    )


def downgrade():
    raise NotImplementedError("Downgrade is not supported.")
