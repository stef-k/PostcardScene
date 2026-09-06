"""Add coalescing catalog refresh request generations without replacing state."""

import sqlalchemy as sa
from alembic import op

revision = "0008_catalog_requests"
down_revision = "0007_media_catalog"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("requested_generation", "handled_request_generation"):
        op.add_column(
            "media_catalog_state",
            sa.Column(
                name,
                sa.BigInteger(),
                sa.CheckConstraint(f"{name} >= 0", name=f"{name}_nonnegative"),
                nullable=False,
                server_default="0",
            ),
        )


def downgrade():
    raise NotImplementedError("Downgrade is not supported.")
