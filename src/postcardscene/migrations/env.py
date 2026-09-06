"""Online migrations use the shared configured connection and transaction."""

from alembic import context

from postcardscene import (  # noqa: F401 -- register migration metadata
    accounts,
    domain,
    settings,
)
from postcardscene.persistence import Base

connection = context.config.attributes.get("connection")
if connection is None or context.is_offline_mode():
    raise RuntimeError("Use PostcardScene's database commands with a live connection.")

context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    render_as_batch=True,
    compare_type=True,
    transactional_ddl=True,
)
with context.begin_transaction():
    context.run_migrations()
