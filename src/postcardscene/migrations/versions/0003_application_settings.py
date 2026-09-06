"""Add the singleton application timezone, initially UTC."""

import sqlalchemy as sa
from alembic import op

revision = "0003_application_settings"
down_revision = "0002_administrator"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table(
        "application_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(255), nullable=False),
        sa.CheckConstraint(
            "id = 1", name=op.f("ck_application_settings_single_settings")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_application_settings"),
    )
    op.bulk_insert(table, [{"id": 1, "timezone": "UTC"}])


def downgrade():
    raise RuntimeError(
        "Downgrades are unsupported; use the verified recovery workflow."
    )
