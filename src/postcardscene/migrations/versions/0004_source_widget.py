"""Add Source and Widget configuration with one optional restrictive source link."""

import sqlalchemy as sa
from alembic import op

revision = "0004_source_widget"
down_revision = "0003_application_settings"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "source",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_source"),
    )
    op.create_table(
        "widget",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["source.id"],
            name="fk_widget_source_id_source",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_widget"),
    )
    op.create_index("ix_widget_source_id", "widget", ["source_id"])


def downgrade():
    raise RuntimeError(
        "Downgrades are unsupported; use the verified recovery workflow."
    )
