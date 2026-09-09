"""Bounded detached database diagnostics and non-executing config inspection."""

import ast
import ipaddress
import os
import stat
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from postcardscene.catalog import MediaCatalogState, catalog_health_counts
from postcardscene.domain import Source
from postcardscene.persistence import Database, DatabaseError
from postcardscene.web.server import serving_options
from postcardscene.web.source_health import health

from . import Check
from .metadata import CONFIG, DATABASE, permissions, read_regular

SNAPSHOT_LIMIT = 64 * 1024 * 1024
SOURCE_LIMIT = 1000


def configuration():
    permissions("config_authority")
    # Executing trusted Python can mutate the host. Inspect literal assignments
    # only; dynamic host configuration remains valid for its owning services but
    # cannot be truthfully evaluated by a read-only command.
    tree = ast.parse(read_regular(CONFIG))
    values = {}
    for node in tree.body:
        if (
            not isinstance(node, ast.Assign)
            or len(node.targets) != 1
            or not isinstance(node.targets[0], ast.Name)
        ):
            raise ValueError("Configuration cannot be inspected without execution.")
        values[node.targets[0].id] = ast.literal_eval(node.value)
    if (
        values.get("DATABASE_PATH", str(DATABASE)) != str(DATABASE)
        or values.get("SESSION_SECRET_PATH", "/var/lib/postcardscene-web/session.key")
        != "/var/lib/postcardscene-web/session.key"
    ):
        raise ValueError("Managed authority mismatch.")
    return values


def web_config(identifier):
    try:
        config = configuration()
    except (OSError, ValueError, SyntaxError):
        return Check(identifier, "unavailable", "config_not_inspectable")
    try:
        options, _ = serving_options(config)
    except (ValueError, TypeError):
        return Check(identifier, "fatal", "config_invalid")
    reason = (
        "same_host_https"
        if options["trusted_proxy"]
        else (
            "direct_loopback"
            if ipaddress.ip_address(options["host"]).is_loopback
            else "direct_remote_opt_in"
        )
    )
    return Check(identifier, "ready", reason)


def _signature(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Invalid database input.")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def snapshot(directory):
    """Copy only main/WAL bytes while stable; SQLite validates the private copy.

    Never open the installed database through SQLite: even mode=ro can create
    sidecars. Reject concurrent main/WAL changes rather than diagnose a torn copy.
    Parent-owned scratch cleanup also runs after a killed check child.
    """
    permissions("durable_permissions")
    permissions("database_permissions")
    paths = (DATABASE, Path(str(DATABASE) + "-wal"))
    before = tuple(_signature(path) for path in paths)
    if before[0] is None or sum(info[2] for info in before if info) > SNAPSHOT_LIMIT:
        raise OSError("Database snapshot unavailable.")
    for path, signature in zip(paths, before, strict=True):
        if signature is not None:
            content = read_regular(path, SNAPSHOT_LIMIT)
            target = directory / path.name
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
    if tuple(_signature(path) for path in paths) != before:
        raise OSError("Database changed during snapshot.")
    return directory / DATABASE.name


def catalog(database):
    with database.transaction() as session:
        sources = list(
            session.scalars(select(Source).order_by(Source.id).limit(SOURCE_LIMIT + 1))
        )
        if len(sources) > SOURCE_LIMIT:
            return Check("catalog", "unavailable", "query_failed")
        attention = 0
        items = 0
        for source in sources:
            if source.kind not in {"local_directory", "mounted_directory"}:
                continue
            state = session.get(MediaCatalogState, source.id)
            label, _ = health(source, state)
            attention += label not in {"Disabled", "Ready"}
            items += sum(catalog_health_counts(session, source.id).values())
        return Check(
            "catalog",
            "degraded" if attention else "ready",
            "catalog_attention" if attention else "ready",
            (("sources", len(sources)), ("items", items), ("attention", attention)),
        )


def database_check(identifier, directory):
    database = None
    try:
        configuration()
        database = Database(snapshot(directory), timeout=0.5)
        database.check()
        if identifier == "catalog":
            return catalog(database)
        return Check(identifier, "ready", "ready")
    except DatabaseError as error:
        # Translate the persistence owner's closed exceptions, never serialize it.
        reason = (
            "schema_incompatible"
            if str(error)
            == "Database schema is incompatible; run the explicit migration workflow."
            else "integrity_failed"
        )
        return Check(identifier, "degraded", reason)
    except (OSError, ValueError, SyntaxError, SQLAlchemyError):
        return Check(identifier, "unavailable", "database_unavailable")
    finally:
        if database is not None:
            database.engine.dispose()
