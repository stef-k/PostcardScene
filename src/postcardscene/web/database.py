"""Thin host CLI integration; sessions stay explicit and framework independent."""

import json
from dataclasses import asdict

import click
from alembic import command
from flask import current_app
from flask.cli import AppGroup
from sqlalchemy.exc import SQLAlchemyError

from postcardscene.persistence import Database, DatabaseError, require_schema
from postcardscene.schema import migration_config, upgrade_database


def init_database(app):
    app.extensions["postcardscene.database"] = Database(app.config["DATABASE_PATH"])
    app.cli.add_command(db)


db = AppGroup("db", help="Explicit database migrations and integrity checks.")


@db.command("upgrade")
def upgrade():
    """Initialize/upgrade to the packaged head. Stop database users first."""
    try:
        identity = upgrade_database(current_app.config["DATABASE_PATH"])
    except (DatabaseError, SQLAlchemyError) as error:
        raise click.ClickException(
            "Database upgrade failed; verify path, schema and storage."
        ) from error
    click.echo(json.dumps(asdict(identity), sort_keys=True))


@db.command("check")
def check():
    """Print application/schema identity after integrity checks pass."""
    try:
        identity = current_app.extensions["postcardscene.database"].check()
    except (DatabaseError, SQLAlchemyError) as error:
        raise click.ClickException(
            "Database check failed; verify path, schema and storage."
        ) from error
    click.echo(json.dumps(asdict(identity), sort_keys=True))


@db.command("revision")
@click.option("--message", "-m", required=True)
def revision(message):
    """Autogenerate a candidate migration in a writable development checkout."""
    database = current_app.extensions["postcardscene.database"]
    with database.engine.begin() as connection:
        require_schema(connection)
        command.revision(
            migration_config(connection), message=message, autogenerate=True
        )
