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

The GitHub Release archive is
`postcardscene-<version>-linux-native.tar.gz`, containing:

```text
postcardscene-<version>-py3-none-any.whl
runtime-requirements.txt
install.py                 # trusted CLI/lifecycle entry point #140/#153
install_inputs.py          # deterministic input/wheel validation #153
install_host.py            # fixed host provisioning primitives #153
install_services.py        # exact managed systemd host support #164
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
gate. Existing managed-state recognition belongs to #140/#142; standalone preflight
accepts clean installation only. #142 adds an installer-internal preserved mode
only after exact removed-state classification; prerequisite policy is unchanged. The plan is a snapshot, not a reusable grant of
filesystem/service authority: the installer must revalidate before mutation.
See [operations](../operations/installation.md#read-only-managed-host-preflight-139) for fixed
packages/paths, command bounds, package evidence and diagnostic interpretation.

### Immutable release bundles (#143)

The release workflow accepts only the exact package-version tag at its recorded
commit, with clean locked validation and pinned uv. It builds the final archive,
external `SHA256SUMS` and minimum candidate notes; publication uses native `gh`
through a draft with write permission confined to the publish job. Existing
releases are never overwritten. The validated package/tag version determines
GitHub prerelease status: pre/dev versions are prereleases and final stable
versions are normal releases, with no manual override. Publication does not close
V0 readiness or the physical/security/recovery gates.

External checksums authenticate downloaded archive bytes before extracted code
execution. The fixed schema-1 manifest records source/tag/version, wheel Python
range, managed target classes, packaged application ID/Alembic head and size/hash
for every other member. `install_inputs.py` validates the exact extracted set and
returns authenticated bytes before host/preflight helpers execute. The validator
itself runs only after all code-owned support/requirements pins pass. Those pins
remain defense in depth; `install.py` never pins the manifest that hashes it.

Archive metadata/order/timestamps are normalized and final bytes are smoked in an
isolated extraction/venv. No optional inventory format is adopted. Repacking
changes candidate authority and requires fresh sums/evidence; reproducibility is
scoped to identical source/tool/compression inputs. See
[release operations](../operations/installation.md#verified-github-release-bundles-143)
for the exact trust and operator sequence. Install/remove/reinstall retain their
existing lifecycle and support split; update and backup/restore remain separate.

### Managed initial installation (#140)

Standalone `install.py install` consumes the validated #138 wheel/requirements
and pinned reviewed support before host mutation. #153 separates deterministic
input/wheel validation into `install_inputs.py` and fixed host provisioning into
`install_host.py`; #164 factors exact systemd authority/retirement into authenticated
`install_services.py`. `install_preflight.py` retains read-only host inspection.
`install.py` retains CLI, phase ordering, recovery dispatch and the initial trust
authority. It reads the entire fixed support/requirements set with bounded,
no-follow, regular-file, one-link checks and authenticates all SHA-256 pins before
executing any helper. Only verified bytes are loaded; no ordinary sibling imports
or preinstalled application are needed. #143 includes these modules in the fixed
manifest/member hashes and clean archive smoke described below. It invokes clean-host
preflight both before fixed package installation and before application-state
provisioning. These installer input pins are not #143's final release manifest;
published-bundle integrity additionally requires the #143 gate above.

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
introduced. Repeat installs reject installed authority unchanged; #142 recognizes
only the exact removed-preserved footprint for compatible reinstall.

See [managed operations](../operations/installation.md#managed-initial-installation-140) for
exact modes, phase/failure recovery and conflict records. Privileged disposable
Linux CI proves real distinct-UID SQLite/WAL/SHM and panel socket cooperation,
private-key/Wayland/device separation and canonical runtime/web service identity.
It does not establish ARM64 provisioning, graphics boot or physical HDMI support.

### Managed removal and reinstall (#142)

`install.py` owns lifecycle dispatch and staged-application compatibility checks;
`install_host.py` owns initial mutating DB/key/migration/admin bootstrap and exact
managed-state inspection, removal and reprovisioning.
The four states are clean, installed managed, removed preserved and partial/unknown.
Normal removal retains `/opt/postcardscene/service-conflicts.json` as root-controlled
ownership evidence alongside config, durable DB, private key and stable users/groups.
Payload, canonical service assets, cache and runtime roots are removed only after
validated ownership and service/process quiescence. Repeated removal is idempotent.

Reinstall classifies preserved state before the internal prerequisite seam, checks
incoming schema/application identity using a detached DB snapshot, and reuses admin
and key authority without migration or initialization. Current conflict state is
atomically recaptured before reserving graphics. Partial layouts fail closed.
See [remove/reinstall operations](../operations/installation.md#managed-remove-and-reinstall-142).
The full fixed support set remains authenticated before any helper execution;
#143 publication and #144 recovery-backed updates remain separate.

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

#158 implements one packaged `postcardscene-backup` CLI and ordinary-Python
`postcardscene.backup.create`, `verify`, and `list_backups` APIs. Manual commands
run as the existing `postcardscene-web` UID, outside Flask/runtime. They neither
gain privileges nor broaden the private signing key's permissions.

The flat gzip/ustar v1 recovery set is exactly `postcardscene.sqlite3`, `config.py`,
`session.key`, and `backup-manifest.json`. Capture validates the managed canonical
config, runtime-owned DB, web-owned key and their parent metadata without following
symlinks. Config uses the installer's literal-assignment convention and must retain
canonical DB/key paths. Existing config/key bytes are copied exactly; neither is
executed or printed. SQLite's read-only online backup API captures the whole DB in
bounded page batches without a checkpoint, migration, vacuum or application-held
SQL transaction. `Database.check()` checks the private snapshot's packaged identity,
WAL mode, quick integrity and foreign keys before publication.

The DB can physically contain `MediaItem`/`MediaCatalogState`; the fixed manifest
classifies these as `regenerable_reconcile_required`. #160 owns invalidating those
observations and requiring fresh Source reconciliation. External media/provider
assets, cache/runtime/browser state, logs, release payload, systemd/PAM/tmpfiles,
and host-specific `service-conflicts.json` are excluded.

Schema-1 manifest fields are closed: archive schema/kind, UTC creation time,
application version, SQLite application ID/schema revision, catalog classification,
and exact DB/config/key names with byte sizes and SHA-256. No manifest self-hash,
host identity, paths, environment or secret values are added. Normalized archive
members have mode 0600, UID/GID 0 and empty owner names. The fixed filename includes
UTC microseconds and application version. A sibling SHA-256 sidecar checks the
final archive bytes; checksums provide integrity, not encryption or proof against
an attacker able to replace both files.

The fixed packaged worker runs with Python isolated imports (`-I -B`) and cwd `/`,
excluding caller CWD, `PYTHONPATH` and user-site packages from its import authority.
Destination filesystem operations run in one disposable subprocess per worker call, bounded
by 30 seconds without progress, 300 seconds overall and one second kill/reap.
Capture also has a 120-second cooperative deadline. Private local `/tmp` scratch
ignores `TMPDIR`; host-local storage there is an operator prerequisite. Sensitive
files use 0600, directories 0700. A killed worker may leave private scratch or
incomplete destination files; kernel-stalled cleanup is explicitly uncertain and
never waits indefinitely. No other destination or storage root is selected.

Publication pins the exact existing absolute destination without symlink traversal,
stages destination-local private files, fsyncs, and uses Linux atomic
`RENAME_NOREPLACE` for each final filename. Unsupported filesystems and collisions
fail closed. Pair publication is not a two-file transaction: only both final files
with a matching sidecar and successful full verification constitute a backup.
Manual creation does not apply retention. Verification first checks the external archive hash,
then bounded fixed structure before extraction to private scratch, exact manifest
and member hashes, and DB integrity. Extra/duplicate/path/link/device/extension
members fail closed. List scans at most 1000 direct entries, ignores foreign/temp
names without opening them, and reports owned pairs as verified, incomplete or
invalid. Exceeding the bound fails rather than presenting a partial inventory.

`VerifiedBackup` is immutable: archive filename/hash, UTC creation time, application
version, SQLite application ID, schema revision and catalog classification. It is
verification evidence only; #144 must reverify at use and #160 owns same-version
restore authority. #165 exposes DB-only authenticated policy/status and private-
snapshot doctor diagnostics; both remain advisory and perform no destination I/O.
Managed destructive restore is implemented by #170 below. #161 composes the
installed recovery evidence; updates (#144) remain unimplemented.
See [manual operations](../operations/installation.md#manual-sensitive-backups-158)
for command usage, size limits and confidentiality responsibilities.


### Concrete update recovery evidence (#161)

The stable #144 gate is exactly `postcardscene.backup.verify(selected_archive)`
returning the frozen `VerifiedBackup`. A recovery point is the **in-memory pair**
`(selected archive path, freshly returned VerifiedBackup)`, with all seven identity
fields listed above. There is no persisted recovery token or additional model.

Immediately before a durable/schema-changing update gate, #144 must select a
concrete path and invoke ordinary strict `verify()`. This reopens its sibling
sidecar/archive through the bounded worker and checks final SHA, fixed structure,
manifest/member hashes and DB identity/integrity. Even a newly created pre-update
backup must pass this same seam; `create()`'s return is not a second update gate.
Carry the path and immutable result through the attempt. Any later recheck must
return the **same complete `VerifiedBackup`**; a valid replacement pair with a
different identity cannot silently replace the selected recovery point.

Persisted `last_success`, `/backup` and doctor state are advisory only and never
select a recovery point or authorize mutation. #144 owns fresh creation versus an
explicitly selected point with acceptable recency; no generic 24/48-hour threshold
is frozen here. Verification does not authorize application/database downgrade;
exact same-version/schema restore remains #160 authority. #161 adds no update code.

### Non-destructive restore intake (#169)

`postcardscene.backup.restore.prepare_restore(archive, staging_root)` is a root-only
ordinary-Python seam consumed by the restore CLI. The caller supplies one fresh,
empty root-owned 0700 direct child of host-local `/tmp`; `TMPDIR` never selects it.
The parent and isolated `restore-stage` worker validate no-follow directory
ownership/mode and the same device/inode identity. Staging must share `/tmp`'s
filesystem; `/tmp` itself must be root-owned 1777. Extraction uses the pinned local
directory descriptor and exclusive private 0600 files.

The existing bounded worker copies the archive and consumes its external checksum
through the destination authority, then uses normal strict current-schema archive
verification. Retention's older-schema ownership seam is never restore authority.
Acceptance additionally requires the installed application version exactly,
`Database.check()`, at least one Administrator, literal-only canonical DB/key config
and a 32-byte signing key. No archived Python executes. Staged files must be private,
root-owned regular one-link files; no services or current durable state are touched.

`PreparedRestore` holds immutable verified backup identity, the exact private root
and its device/inode, plus fixed file sizes/hashes for the copied archive, manifest,
DB, config and key. Paths and member identities are excluded from repr; contents
are never returned. `revalidate_restore(candidate)` compares local authority and
all fixed file hashes immediately before target staging. It needs no backup
destination access, including after that destination disappears. This is an
in-process candidate, not a serialized token or permission to replace host state.

Failure cleans only accepted local scratch best-effort and returns fixed safe
categories. `restore_cleanup_uncertain` preserves confidentiality while reporting
unproven local cleanup; `worker_cleanup_uncertain` leaves scratch to a potentially
kill-pending worker. Successful scratch lifetime belongs to the restore caller.


### Destructive managed restore (#170)

The existing CLI adds only root-only `restore <archive>`. `restore_execute` consumes
#169's local candidate and uses a fixed packaged `restore_host` boundary, independent
of extracted installer support. Read-only release/assets/conflict metadata checks
are reused; current config/DB/key contents need not be healthy. Exact target and
unit authority remains mandatory, including only three services and two backup
auxiliary units. No destination I/O follows candidate preparation.

Root takes the same #164 web-owned/shared-group/0600 one-link no-follow
lock. If absent, root creates it exclusively under the validated private parent,
sets its exact authority and validates it normally. An existing inode is never
replaced or repaired; the public entrypoint remains web-only. The lock
precedes all systemd changes and covers stop/disable, quiescence, raw rollback
capture, replacement, validation, catalog invalidation and service activation.
Timer and services remain persistently disabled throughout the destructive window.
Handled interruptions use the same bounded failure path; SIGKILL/power loss relies
on persistent disabled units and requires operator recovery.

`restore_files` captures bounded raw bytes and metadata to root-private local
rollback scratch, including existing sidecars, without opening current SQLite.
The candidate is revalidated immediately before per-target-filesystem staging;
copy-time size/hash checks, final owners/modes, fsync and atomic replacement protect
each incoming config/DB/key. No archived sidecar is restored. Pre-commit failure
attempts exact byte/metadata rollback, retires every unit even after proven rollback,
and preserves scratch when manual recovery is needed. Uncertain cleanup is visible.
Secondary cleanup failure preserves the primary restore error and adds the fixed
JSON field `cleanup: restore_cleanup_uncertain`; it never hides host state.

Commit follows canonical non-executing config validation, exact key authority,
current DB identity/integrity/Administrator checks and committed catalog
invalidation. The catalog-owned bulk helper is equivalent to superseding each
state with invalidation: delete MediaItems, advance/completely supersede generation,
handle current requests, reset result to `never_scanned` and clear attempt/success
freshness. All non-catalog durable rows remain. No scan is performed.

After commit, activation failure retains valid recovered data and attempts full
fail-stopped retirement. Successful activation enables/starts graphics/runtime/web,
enables the timer, releases the shared lock, then starts/requires the timer active
regardless of backup policy. One engine covers in-place and exact-matching fresh
replacement installation. Host-specific installation state remains untouched;
#161 exercises one canonical backup through in-place recovery and test-only host
loss followed by a clean exact-release install and replacement-host recovery.
The existing installed-Linux lane verifies original durable state and exact config/
key bytes, catalog invalidation, scheduled retention and install/doctor authority.
Only graphics activation is simulated; #144 remains unimplemented.


### Persisted backup policy and daily due state (#163)

Explicit migration `0012_backup_policy` follows `0011_display_power_settings` and
adds one `backup_policy` row (`id = 1`), preserving existing appliance state.
`postcardscene.backup_policy` owns frozen policy/history/status values and short
DB-only transactions. Defaults are disabled, no destination, local hour 3 and
retention count 7. Replacement requires a real bool, an absolute nonempty path
of at most 4096 characters (nullable only while disabled; no NUL), integer hour
0–23 and retention 1–30. Validation never probes or resolves the destination;
#158's worker remains accessibility and filesystem authority. Malformed stored
policy/history fails closed with `DatabaseError`, rather than being defaulted.

`evaluate_backup_due` consumes an explicit aware UTC datetime, the existing
application IANA timezone, policy and history. It owes only the current local
date after its configured hour. A skipped DST hour is caught on a later invocation
that date; a repeated hour cannot duplicate a satisfied date. A satisfied ISO date
greater than or equal to today suppresses work after backward clock/date movement.
Downtime owes only today, without replay. Disable/re-enable and destination/hour/
retention edits preserve satisfaction. Invalid/unavailable timezone yields fixed
`timezone_unavailable`, never UTC fallback. `get_backup_status` reads the existing
application timezone and invokes this same evaluator; destination text is omitted
unless its authenticated caller explicitly requests it. #165 consumes this seam
from `/backup` and doctor's private snapshot; neither performs destination I/O.

`record_backup_attempt` records a nonnegative signed-64-bit UTC epoch-ns timestamp
and conservatively marks `failed` until verified success, including on interruption.
`record_backup_success` consumes an already obtained `VerifiedBackup`, explicit
success-completion epoch-ns and the scheduled obligation's ISO local date. It stores
only that date/time and the bounded exact #158 archive basename, refusing backward
satisfaction movement. The execution owner must record the attempt first and owns
serialization; no create/verify/list operation occurs in these transactions.
The closed results are `never`, `failed`, `ready` and `ready_retention_degraded`;
the last denotes verified success with failed retention cleanup. Later attempts or
failures preserve the last known-good success identity. Persisted history is
advisory operator evidence, never a verified recovery point: restore/update must
reverify the concrete archive and sidecar at use. #164 owns execution and retention.

### Scheduled execution and auxiliary lifecycle (#164)

`postcardscene-backup scheduled` holds one nonblocking `flock` on the local
`/var/lib/postcardscene-web/backup.lock`: regular, no-follow, one link, web-owned,
shared group, mode 0600 inside the protected private root. Manual create takes the
same lock; scheduled calls the already-locked create primitive and retains the
lock continuously through retention. Mutation workers inherit that same open
lock so controller death or kernel-stuck cleanup cannot permit a competing writer.
The lock is never unlinked or archived and may survive remove/reinstall.

Each invocation reads one immutable #163 status snapshot with explicit UTC now.
Disabled/before-hour/satisfied return success without destination I/O. Unavailable
policy/time fails with a fixed safe category. A due invocation freezes destination,
retention count and due local date; it records one attempt, creates outside every
DB transaction, then records the already-verified identity for that captured date.
Retention uses the same destination/count. Failure preserves success and records
`ready_retention_degraded`; creation failure preserves the previous success identity.

Retention stays inside the existing isolated, bounded worker. At most 1000 direct
entries are enumerated; exact complete pairs require bounded checksum, flat archive,
manifest and member-hash evidence, without historical SQLite integrity scans.
V1 ownership accepts schema identifiers of 1–32 ASCII letters/digits/underscores
(starting with a letter/digit), including older revisions. Normal verification
keeps its separate current-schema equality check; retention ownership does not
establish recovery compatibility.
Deterministic oldest-first deletion retains the newest N owned pairs, reserving
one slot for the new verified backup even after a clock reversal. Foreign, temporary,
incomplete and malformed entries confer no deletion authority. Exact no-follow
file identities are rechecked immediately before unlink; uncertainty stops deletion.

The packaged backup oneshot/timer are auxiliary managed units. The three-service
`install_host.SERVICES` contract stays graphics/runtime/web. The hourly persistent
timer is enabled and started after coherent payload/schema activation on every
install/reinstall, including disabled policy. The web-UID/shared-group oneshot has
umask 0077, no capabilities/device access and a 660-second start timeout covering
both bounded worker calls. Destination access uses ordinary DAC, without a
policy-dependent unit path allowlist or mount/credential provisioning.

Clean preflight requires both units absent; installed classification checks exact
assets, effective unit authority and enabled/active timer without requiring the
oneshot active. Unknown PostcardScene units remain rejected. Removal retires the
timer and oneshot before service-UID quiescence, removes auxiliary assets and retains
archives, policy/history and private state. Failure recovery attempts timer stop,
disable and oneshot stop before always-on service recovery, preserving uncertain
assets for diagnosis. Removed-preserved requires absent/inactive auxiliary authority;
compatible reinstall restores the timer without changing policy/history.

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
