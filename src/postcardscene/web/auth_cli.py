"""Local commands require the service account's host/filesystem authority."""

import click
from flask import current_app
from flask.cli import AppGroup
from sqlalchemy.exc import SQLAlchemyError

from postcardscene.accounts import set_password
from postcardscene.persistence import DatabaseError
from postcardscene.session_secret import initialize_secret, read_secret

auth_cli = AppGroup("auth", help="Host-authorized administrator setup and recovery.")


@auth_cli.command("init-secret")
def init_secret():
    """Provision a signing key once, without printing or replacing it."""
    try:
        initialize_secret(current_app.config["SESSION_SECRET_PATH"])
    except (OSError, ValueError) as error:
        raise click.ClickException(
            "Signing-key setup failed; verify private path and ownership."
        ) from error
    click.echo("Signing key ready.")


def change_password(username, initial):
    try:
        read_secret(current_app.config["SESSION_SECRET_PATH"])
        password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
        set_password(
            current_app.extensions["postcardscene.database"],
            username,
            password,
            initial=initial,
        )
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    except (OSError, DatabaseError, SQLAlchemyError) as error:
        raise click.ClickException(
            "Administrator update failed; verify signing key, database and migrations."
        ) from error
    click.echo(
        "Administrator created." if initial else "Password reset; all sessions revoked."
    )


@auth_cli.command("create-admin")
@click.option("--username", prompt=True)
def create_admin(username):
    """Create the initial administrator; refuses an existing account."""
    change_password(username, True)


@auth_cli.command("reset-password")
@click.option("--username", prompt=True)
def reset_password(username):
    """Reset the existing administrator's password and revoke every session."""
    change_password(username, False)
