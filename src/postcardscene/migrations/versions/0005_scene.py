"""Add Scenes and owned, canonically ordered Widget placements."""

import sqlalchemy as sa
from alembic import op

revision = "0005_scene"
down_revision = "0004_source_widget"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scene",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("layout", sa.String(64), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_scene"),
    )
    op.create_table(
        "scene_placement",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scene_id", sa.Integer(), nullable=False),
        sa.Column("widget_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("region", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_scene_placement"),
        sa.ForeignKeyConstraint(
            ["scene_id"],
            ["scene.id"],
            name="fk_scene_placement_scene_id_scene",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["widget_id"],
            ["widget.id"],
            name="fk_scene_placement_widget_id_widget",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("scene_id", "position", name="uq_scene_placement_position"),
        sa.UniqueConstraint("scene_id", "region", name="uq_scene_placement_region"),
        sa.UniqueConstraint("scene_id", "widget_id", name="uq_scene_placement_widget"),
    )
    op.create_index("ix_scene_placement_widget_id", "scene_placement", ["widget_id"])


def downgrade():
    raise RuntimeError(
        "Downgrades are unsupported; use the verified recovery workflow."
    )
