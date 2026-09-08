# Installation, recovery and operations

Package bootstrap, shared persistence/migrations, reliability, release/install/update, backup/restore and release evidence.

Read the [architecture entry point and map](../architecture.md) first. Together,
the overview and linked subsystem documents form the architecture authority.

Deployment instructions remain in [Operations](../operations.md); product usage and setup remain in the [README](../../README.md).

## Bootstrap package and tooling

Issue #12 establishes one `postcardscene` distribution under `src/postcardscene`,
with side-effect-free `web` and `runtime` namespaces. Python 3.11 is the bootstrap
compatibility floor; #26 owns the supported release matrix and version identity.
The initial package version is `0.0.0`. Issue #13 adds Flask as the control-plane
dependency; the core and runtime namespaces remain independent of Flask.

uv and the committed `uv.lock` own project environments and dependency resolution;
`uv_build` owns builds. Ruff is the sole project-local formatter/linter and pytest
the test runner. The bootstrap CI gate checks locked sync, formatting, lint,
tests, and builds on Python 3.11. No mandatory static type checker is introduced;
revisit typing when substantive contracts warrant it.

## Shared persistence

Issue #14 freezes the shared persistence foundation in `postcardscene.persistence`:
ordinary SQLAlchemy 2.x models/metadata and explicit unit-of-work sessions, usable
without Flask. Alembic owns all schema changes through packaged online migrations;
a thin Flask CLI adapter exposes upgrade/check/revision commands. No extension
owns a separate Flask-only model or session layer.

The database is an absolute host-local file, defaulting to
`/var/lib/postcardscene/postcardscene.sqlite3`, configurable with `DATABASE_PATH`.
The operator/installer provisions its private parent directory; network storage
is suitable for external media, not this database. Normal access opens an existing
file only. App construction is lazy and performs no schema work; transactions
reject missing/unrecognized/incompatible databases before application use.

Connection policy is SQLite WAL, `foreign_keys=ON`, `synchronous=FULL`, a five-second
busy timeout, and automatic WAL checkpoints at 1000 pages. Only explicit migration
enables WAL; ordinary access validates it. Use SQLAlchemy `NullPool` so each unit
of work gets a fresh connection, closed on completion. Each process constructs
its own engine after process creation; never pass connections/sessions across
process boundaries. SQLAlchemy emits explicit deferred `BEGIN` for consistent
transactional reads and DDL on Python 3.11 and later, overriding sqlite3 legacy
transaction handling. Do not override isolation level on these connections.

WAL permits readers alongside one writer, not concurrent writers. All owners use
short transactions; prepare network/media work outside them and commit catalog
batches between cancellation checks. Reads hold snapshots until completion;
long readers can delay checkpointing and grow the WAL. A competing write can fail
with SQLite BUSY/LOCKED after the timeout, or immediately for a stale read snapshot.
Roll back and surface the failure. Later owning features may retry the entire
idempotent unit with a bounded policy, never just a statement or a tight loop.
The foundation does not retry automatically or use SQLite as a work queue.
Independent-engine tests exercise reader visibility, writer contention and
recovery on a real local SQLite file; they do not claim network-filesystem support.

`Database.transaction()` owns commit/rollback/close, with autoflush disabled and
normal expire-on-commit behavior. No implicit Flask teardown commits or persistent
runtime sessions are allowed. Future models inherit the common `Base`, including
stable constraint naming for migration generation. No domain tables are needed
for the baseline.

Identity combines the installed `postcardscene` distribution version (from the
single `pyproject.toml` version), SQLite `application_id=0x5053434E` (`PSCN`), and
the exact Alembic revision (`0001_baseline` initially). Application access accepts
only the packaged expected revision. Migration accepts an empty file or a known
single revision with matching file identity; it refuses unknown/unversioned
nonempty databases. Revisions are immutable once shipped. Future schema changes
update the expected revision and must demonstrate preservation across upgrade.
Identity supports comparison/recording for #26/#27 but is not by itself proof of
backup or cross-version restore compatibility.

`Database.check()` validates identity/WAL, SQLite quick integrity and foreign-key
integrity without repair. Compatibility/integrity failures raise `DatabaseError`;
SQLAlchemy storage/constraint/locking exceptions remain distinguishable to callers.
SQL parameter values are hidden in SQLAlchemy exception formatting; raw exceptions
still belong only in protected diagnostics. This is a primitive for later
operator/backup checks, not a status UI or recovery implementation. Migrations
require all database users stopped; production backup/update orchestration stays
with #26/#27. See README for initialization and development commands.

## Reliability, logging, and resource bounds

PostcardScene is intended to run unattended for long periods.

The runtime must tolerate:

- service restart
- host reboot/power loss
- display absent at boot
- display disconnect/reconnect
- NAS temporarily offline or unresponsive
- internet/provider outage
- Chromium crash/hang
- mpv crash/hang
- malformed/unsupported media
- missing files
- renderer stall
- catalog interruption/restart

The control plane should remain reachable whenever the host itself is healthy.

Use systemd for service supervision. Logs should be diagnosable but have bounded disk growth. Cache growth should be bounded/cleanable. Low disk-space state should be visible through status/doctor paths rather than discovered only after corruption/failure.

## Release, installation, and update boundary

V0 targets a managed native Linux installation rather than requiring users to deploy from a Git checkout.

Expected conceptual layout separates:

```text
application payload
configuration
secrets
SQLite/durable application state
replaceable caches
transient runtime state
logs
backup destination
```

Normal managed installation should use an isolated project-owned Python environment or another equally safe supported mechanism rather than modifying distro-owned Python through privileged pip installation.

The exact release artifact format remains open: wheel, archive, or another small Python-native shape may be selected by #26.

Stable releases should be tied to immutable source/tag/version identity and final artifact checksums. Clean-install smoke tests, explicit migrations, diagnostics, backup-backed forward updates, and safe removal/reinstall are V0 lifecycle requirements.

V0 does not require an APT repository, mandatory `.deb`, Docker, an auto-update daemon, or a transactional application/database rollback engine.

## Backup and restore boundary

PostcardScene backup owns PostcardScene durable state, not the user's original media libraries.

The V0 recovery contract must classify:

- SQLite database
- installation-owned secret/key material required for protected state, or credentials explicitly requiring re-entry
- any application-owned durable assets
- media catalog as regenerable derived state; restore requires fresh Source reconciliation
- manifest with application/schema/archive identity
- checksums

It excludes external media libraries, replaceable caches and the isolated
untrusted-web Chromium profile (including ordinary site state) by default.

Backup creation must use a SQLite-consistent method, publish atomically, verify integrity, and support bounded retention.

Restore must be tested both:

1. in place; and
2. onto a clean supported replacement host.

An older backup is not automatically a safe application rollback. Unknown application/schema compatibility must fail closed with actionable recovery guidance.

## Documentation and release evidence

Documentation is part of feature completion.

Before V0 closes, a new administrator must be able to determine support, install, configure, secure, diagnose, update, remove/reinstall, back up, and recover the appliance without reading implementation code.

Hardware claims should distinguish physically tested configurations from intended/untested possibilities.

PostcardScene is licensed under the MIT License. Before a public distributable release, the project must also provide any required third-party notices/attributions.

The V0 tracker/release issue owns exact-candidate closure evidence for:

- source/tag/version/artifact checksum
- CI/test/lint/security state
- schema/migration identity
- clean installation
- physical graphics/4K claims
- backup/restore
- security review
- operator docs/license/notices
- no known release blocker

