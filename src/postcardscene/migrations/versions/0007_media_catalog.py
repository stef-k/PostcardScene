"""Add regenerable Source-owned filesystem catalog and reconciliation state."""

import sqlalchemy as sa
from alembic import op

revision = "0007_media_catalog"
down_revision = "0006_sequence"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "media_item",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("source.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relative_path", sa.String(4096), nullable=False),
        sa.Column("media_type", sa.String(16), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("seen_generation", sa.BigInteger(), nullable=False),
        sa.Column("display_width", sa.Integer(), nullable=True),
        sa.Column("display_height", sa.Integer(), nullable=True),
        sa.Column("orientation", sa.String(16), nullable=True),
        sa.Column("metadata_status", sa.String(16), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint(
            "source_id", "relative_path", name="uq_media_item_identity"
        ),
    )
    op.create_index(
        "ix_media_item_generation", "media_item", ["source_id", "seen_generation"]
    )
    op.create_index(
        "ix_media_item_selection",
        "media_item",
        ["source_id", "media_type", "orientation"],
    )
    op.create_table(
        "media_catalog_state",
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("source.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("scan_generation", sa.BigInteger(), nullable=False),
        sa.Column("completed_generation", sa.BigInteger(), nullable=False),
        sa.Column("last_result", sa.String(16), nullable=False),
        sa.Column("last_attempt_ns", sa.BigInteger(), nullable=True),
        sa.Column("last_success_ns", sa.BigInteger(), nullable=True),
    )


def downgrade():
    raise NotImplementedError("Downgrade is not supported.")
