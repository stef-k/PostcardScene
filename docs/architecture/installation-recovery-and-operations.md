# Installation, recovery and operations

Package bootstrap, shared persistence/migrations, reliability, release/install/update, backup/restore and release evidence.

Read the [architecture entry point and map](../architecture.md) first. Together,
the overview and linked subsystem documents form the architecture authority.

Deployment instructions remain in [Installation and diagnostics](../operations/installation.md); product usage and setup remain in the [README](../../README.md).

## Bootstrap package and tooling

Issue #12 establishes one `postcardscene` distribution under `src/postcardscene`,
with side-effect-free `web` and `runtime` namespaces. #138 freezes released
CPython 3.11–3.14 support (`>=3.11,<3.15`), excluding 3.15 prerelease. Static
`[project].version` in `pyproject.toml` is the sole application version authority,
currently `0.1.0.dev0`; the first stable V0 line is `0.1.x`. No runtime constant,
Git-derived version or environment override is added. Issue #13 adds Flask as the control-plane
dependency; the core and runtime namespaces remain independent of Flask.

uv and the committed `uv.lock` own project environments and dependency resolution;
`uv_build` owns builds. Ruff is the sole project-local formatter/linter and pytest
the test runner. Quality checks locked sync, formatting, lint and the full tests on every supported
Python series, with one build/package smoke on 3.11. Generic Linux CI proves
Python portability only, not Raspberry Pi ARM64, graphics, codecs or devices. No mandatory static type checker is introduced;
revisit typing when substantive contracts warrant it.

## Shared persistence

Issue #14 freezes the shared persistence foundation in `postcardscene.persistence`:
ordinary SQLAlchemy 2.x models/metadata and explicit unit-of-work sessions, usable
without Flask. Alembic owns all schema changes through packaged online migrations;
a thin Flask CLI adapter exposes upgrade/check/revision commands. No extension
owns a separate Flask-only model or session layer.

The database is an absolute host-local file, defaulting to
`/var/lib/postcardscene/postcardscene.sqlite3`, configurable with `DATABASE_PATH`.
The operator/installer provisions its shared application-group parent directory;
network storage is suitable for external media, not this database. Normal access opens an existing
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

#122 freezes `postcardscene-runtime.service` supervision and the installed
[path/logging contract](../operations/runtime.md#installed-runtime-service-122).
`/etc/postcardscene/config.py` is the shared trusted operator configuration for
installed runtime and web services. #131 supplies foreground `postcardscene-web`
using Waitress; #125 packages its independently supervised
[web unit](../operations/runtime.md#installed-web-service-125), with persistent writes
limited to shared state and read-only private signing authority. Web runs as `postcardscene-web` with primary
GID `postcardscene`, without runtime device/Wayland authority. Shared SQLite
under `/var/lib/postcardscene` uses group `postcardscene`; #26 owns exact modes,
umask and two-UID WAL/SHM evidence. Private durable signing authority is instead
`/var/lib/postcardscene-web/session.key`, web-owned 0600 under web-owned 0700 parent.
#26 initializes it through existing auth authority as the web UID; RuntimeHost
never reads it. #27 must explicitly back up/restore this sensitive authority.
See the [serving/identity contract](control-plane-and-security.md#production-serving-131).
`/var/lib/postcardscene` is durable,
`/var/cache/postcardscene` replaceable, `/run/postcardscene` transient, and
`/run/postcardscene-wayland` independently graphics-owned. Application startup
does not recursively provision or repair these roots; #26 owns provisioning.

V0 runtime lifecycle logs use stdlib logging and journald only, with fixed safe
messages and no raw exceptions/tracebacks or host/configuration values. Distro
journal limits own persistence/rotation; no duplicate application log tree or
global journald configuration mutation is introduced. Cache growth should be
bounded/cleanable. Low disk-space state should be visible through status/doctor
paths rather than discovered only after corruption/failure.

## Owned storage health (#123)

`postcardscene.resource_health` supplies immutable ordinary-Python snapshots for
explicit trusted durable-state, cache and transient-runtime roots. Overview checks
only the installed `/var/lib/postcardscene`, `/var/cache/postcardscene` and
`/run/postcardscene` roots, in that order. Custom database paths remain covered by
the separate `Database.check()` row; no parent-directory or mount discovery occurs.
The seam can be reused by #26 doctor with explicitly supplied owned roots.

Each direct `os.statvfs()` call uses `f_blocks * f_frsize` capacity and
`f_bavail * f_frsize` free bytes: space available to the unprivileged application,
excluding root-reserved blocks. Only zero fragment size falls back to `f_bsize`.
Counters and byte products must fit unsigned 64-bit values. Invalid integer
values (including booleans), nonpositive capacity, negative free space, free above
capacity and stat/arithmetic failures return unavailable with fixed safe reasons.

Frozen V0 thresholds are critical first: free bytes **< 256 * 1024 * 1024** OR
free/capacity **< 2%**; otherwise warning if free bytes **< 1024 * 1024 * 1024** OR
free/capacity **< 5%**; otherwise healthy. Comparisons are strict: equality alone
does not trigger that threshold, but the other dimension can. Integer
cross-products compare the ratios exactly without floating-point rounding.
Thresholds are not configurable.

One authenticated Owned storage row presents the worst state using
critical > warning > unavailable > healthy, with durable state first in details
and each unavailable resource still named. It exposes rounded free GiB and fixed
states, never paths or exceptions. This is a current host snapshot and advisory
only: no persistence, background monitor, recursive sizing, cleanup or mutation.
External media/NAS and backup destinations (#27) are excluded. Journald remains
host/systemd retention authority; #26 owns doctor/service/journal installation
checks and #126 owns later cache cleanup.

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

### Deterministic release inputs (#138)

The canonical application payload is one standards-compliant
`postcardscene-<version>-py3-none-any.whl`, built from an immutable source candidate
with `uv build --wheel --no-sources`. A native/platform wheel fails validation.
The wheel carries Python modules/console scripts, Alembic migrations, Flask
static/templates, runtime/web units and graphics systemd/PAM/labwc/helper assets.
Payload validation compares all package files with tracked source; no generated
host configuration, secrets, database, cache/profile, backup or credentials belong
in it. Release secret/dependency auditing remains #29's responsibility.

`uv.lock` remains resolution authority. The generated `runtime-requirements.txt`
is the exact pip-compatible runtime-only export, with hashes and without the local
project or development dependencies. A stale export fails the repository package
check. Clean installation uses `pip install --require-hashes --only-binary=:all:
-r runtime-requirements.txt`, then installs the application wheel `--no-deps`.
Targets never resolve fresh unconstrained dependencies or fall back to source
builds; unavailable compatible wheels fail visibly.

Future release tags must be exactly `v<project.version>`, such as `v0.1.0rc1` or
`v0.1.0`. Tag, wheel metadata, installed `importlib.metadata.version("postcardscene")`,
Overview and release manifest must agree exactly before publication; #143 owns
that publication gate and final manifest/checksums.

The future GitHub Release archive is
`postcardscene-<version>-linux-native.tar.gz`, containing:

```text
postcardscene-<version>-py3-none-any.whl
runtime-requirements.txt
install.py                 # trusted CLI/lifecycle entry point #140/#153
install_inputs.py          # deterministic input/wheel validation #153
install_host.py            # fixed host provisioning primitives #153
install_preflight.py       # standalone read-only install support #139
release-manifest.json      # release child #143
```

Only explicitly documented release metadata/checksums may extend that shape.
`install.py` will be a standalone stdlib-oriented entry point: no source checkout
or preinstalled PostcardScene import is required. #138 supplies inputs and freezes
the initial layout, expanded by #153 with the two reviewed support modules;
it does not create an installer, mutate a host, migrate production
state or publish a release. GitHub Releases is the V0 channel; no PyPI publication,
`.deb`, APT repository, Docker or frontend build is introduced. Repacking different
bytes creates a new artifact candidate requiring new checksum/evidence.

Stable releases should be tied to immutable source/tag/version identity and final artifact checksums. Clean-install smoke tests, explicit migrations, diagnostics, backup-backed forward updates, and safe removal/reinstall are V0 lifecycle requirements.

V0 does not require an APT repository, mandatory `.deb`, Docker, an auto-update daemon, or a transactional application/database rollback engine.

### Managed-host preflight (#139)

The standalone stdlib `install_preflight.py` freezes the read-only gate consumed
by #140's native installer and shipped beside it by #143. Managed V0 targets are
Raspberry Pi 4/5-class ARM64 with Ubuntu Server 24.04/Noble or 26.04/Resolute LTS,
or Raspberry Pi OS 64-bit / Debian 13 Trixie. Point releases keep those identities;
generic x86 CI remains portability evidence. Distro differences are provisioning
data, never alternate application or graphics architectures.

The frozen result contains either safe failure categories with no plan, or a
closed immutable package/tool/seat/action plan. Ubuntu uses archive tools and
Canonical's Chromium snap; Debian/RPi uses native Chromium. Distro CPython
3.11–3.14 with venv/ensurepip is required before provisioning. Default logind/PAM
and explicit seatd retain #62 ownership. Missing installable tools are actionable;
unknown package authority, unsafe/non-local roots, existing unrecognized
accounts/installations/units and unavailable host facts fail closed. Tty1/getty
and display-manager conflicts remain explicit actions for the installer.

No package/user/directory/config/venv/service/database mutation occurs in this
gate. Existing managed-state recognition belongs to #140/#142; preflight currently
accepts clean installation only. The plan is a snapshot, not a reusable grant of
filesystem/service authority: the installer must revalidate before mutation.
See [operations](../operations/installation.md#read-only-managed-host-preflight-139) for fixed
packages/paths, command bounds, package evidence and diagnostic interpretation.

### Managed initial installation (#140)

Standalone `install.py install` consumes the validated #138 wheel/requirements
and pinned reviewed support before host mutation. #153 separates deterministic
input/wheel validation into `install_inputs.py` and fixed host provisioning into
`install_host.py`; `install_preflight.py` retains read-only host inspection.
`install.py` retains CLI, phase ordering, recovery dispatch and the initial trust
authority. It reads the entire fixed support/requirements set with bounded,
no-follow, regular-file, one-link checks and authenticates all SHA-256 pins before
executing any helper. Only verified bytes are loaded; no ordinary sibling imports
or preinstalled application are needed. #143 must include both new modules in
its later fixed manifest/member hashes and clean archive smoke. It invokes clean-host
preflight both before fixed package installation and before application-state
provisioning. These installer input pins are not #143's final release manifest;
published-bundle schema/member authentication remains with #143.

The initial path selects logind/PAM, stages a root-controlled versioned venv,
provisions distinct locked runtime/web UIDs with shared primary group and setgid
SQLite state, then uses existing web-UID secret/migration/admin CLI authority
with umask `0007`. The runtime UID exclusively reserves the new empty DB file
with mode `0660` before Alembic writes its content; SQLite's `0644` initial file
mode cannot gain group write from umask alone. Versioned units/PAM and wheel-local labwc assets remain their
owning subsystems' policies. Root-owned web/runtime umask drop-ins grant shared
SQLite sidecar access without broadening private signing-key authority. Only
successful bootstrap permits atomic active-symlink publication and service
enablement/start. No legacy adoption, credential reset, update or DB rollback is
introduced. Repeat installs reject existing authority unchanged.

See [managed operations](../operations/installation.md#managed-initial-installation-140) for
exact modes, phase/failure recovery and conflict records. Privileged disposable
Linux CI proves real distinct-UID SQLite/WAL/SHM and panel socket cooperation,
private-key/Wayland/device separation and canonical runtime/web service identity.
It does not establish ARM64 provisioning, graphics boot or physical HDMI support.

## Installed doctor (#141)

`postcardscene-doctor` owns bounded, read-only administrative aggregation of the
existing persistence, persisted catalog, resource, serving and graphics seams.
Its closed immutable check model supplies both human and JSON output. Exact
#140 layout metadata and the explicit installer console-script set include doctor;
#143 retains final release-manifest/member-hash authority.

Each check runs in a short-lived isolated Linux child with a five-second deadline,
fixed output vocabulary and bounded tool capture. It never executes Python host
configuration: literal assignments feed the existing serving validator; dynamic
configuration is unavailable to this read-only observer. DB checks use a bounded
private main/WAL copy, rejecting concurrent changes, so SQLite cannot create or
modify installed sidecars. Only disposable scratch is writable and parent-owned
cleanup survives check timeout. This sample is not a backup or recovery claim.
Graphics ownership is not bypassed, and executable tool prerequisites do not
establish device/physical capability. See [doctor operations](../operations/installation.md#installed-read-only-diagnostics-141)
for limits, result/exit categories and repair/evidence ownership.

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
