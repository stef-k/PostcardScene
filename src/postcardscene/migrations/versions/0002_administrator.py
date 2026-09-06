"""Add the single local administrator and revocable session identity."""

import sqlalchemy as sa
from alembic import op

revision = "0002_administrator"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "administrator",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "id = 1", name=op.f("ck_administrator_single_administrator")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_administrator"),
        sa.UniqueConstraint("username", name="uq_administrator_username"),
        sa.UniqueConstraint("session_id", name="uq_administrator_session_id"),
    )


def downgrade():
    raise RuntimeError(
        "Downgrades are unsupported; use the verified recovery workflow."
    )
