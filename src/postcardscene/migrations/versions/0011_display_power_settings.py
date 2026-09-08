"""Persist bounded display power and static protection policy only."""

import sqlalchemy as sa
from alembic import op

revision = "0011_display_power_settings"
down_revision = "0010_operating_schedule"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("application_settings") as batch:
        batch.add_column(
            sa.Column(
                "display_power_backend",
                sa.String(6),
                nullable=False,
                server_default="auto",
            )
        )
        batch.add_column(
            sa.Column(
                "display_wake_delay_seconds",
                sa.Integer(),
                nullable=False,
                server_default="5",
            )
        )
        batch.add_column(
            sa.Column(
                "maximum_static_dwell_seconds",
                sa.Integer(),
                nullable=False,
                server_default="1800",
            )
        )
        batch.create_check_constraint(
            "display_power_backend_choice",
            "display_power_backend IN ('auto', 'cec', 'ddc', 'signal')",
        )
        batch.create_check_constraint(
            "display_wake_delay_range",
            "typeof(display_wake_delay_seconds) = 'integer' AND display_wake_delay_seconds BETWEEN 0 AND 30",
        )
        batch.create_check_constraint(
            "maximum_static_dwell_range",
            "typeof(maximum_static_dwell_seconds) = 'integer' AND maximum_static_dwell_seconds BETWEEN 300 AND 14400",
        )


def downgrade():
    raise NotImplementedError("Downgrade is not supported.")
