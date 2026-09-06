"""Establish PostcardScene file identity; domain tables belong to later issues."""

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("PRAGMA application_id=1347633998")  # 0x5053434E / PSCN


def downgrade():
    raise RuntimeError(
        "Downgrades are unsupported; use the verified recovery workflow."
    )
