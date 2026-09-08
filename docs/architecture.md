# Architecture

This document captures the architectural direction for PostcardScene. It preserves decisions that should constrain implementation while leaving genuinely unresolved choices open until the issue that owns them has enough evidence to decide.

## 1. Product shape

PostcardScene is a self-hosted ambient media and information display appliance, not merely a slideshow and not primarily a web application.

Photography is the primary use case and travel is an important part of the project's identity, but the runtime is intentionally broader. It should be able to combine:

- images
- portrait image pairs
- video and optional audio
- web pages
- live maps
- weather
- news/RSS
- location/context data
- generic remote data
- future information widgets

The initial target is a Raspberry Pi-class ARM64 Linux host connected to a modern television or monitor. 1080p is the minimum intended display resolution. A 4K path is preferred but must be physically validated on representative hardware before being described as supported.

The implementation should remain portable enough to run on other supported Linux systems instead of hard-coding application logic to Raspberry Pi hardware.

## 2. Design principles

### The display experience is primary

The web application exists to configure, observe, and control the display appliance. The long-running runtime/player is the product execution environment.

### Keep the server architecture proportionate

Complexity should live in content selection, composition, rendering, media indexing, scheduling, context, hardware integration, recovery, and reliability rather than in unnecessary web-framework or distributed-system machinery.

### Graceful degradation

A disconnected NAS, malformed media item, crashed browser, unavailable display, failed power-control backend, or future external-provider outage must not unnecessarily take down the entire appliance.

### Long-running work does not belong in web requests

Source reconciliation, scheduling, renderer supervision, and later cached-provider refresh are long-running application work. They belong to the runtime/player process or a narrowly factored component it owns, not to Flask request handlers.

V0 does not require Redis, Celery, RabbitMQ, a distributed queue, or an additional always-on worker service.

### Configuration through the product

Important normal operating behavior should be tunable from the authenticated PostcardScene web interface rather than requiring users to edit service files or scripts.

Host-level installation, recovery, device permissions, and other privileged operations remain bounded administrative responsibilities rather than ordinary web actions.

### Security is an appliance boundary

PostcardScene deliberately combines remote administration, filesystem access, arbitrary administrator-selected web content, subprocesses, and display hardware. These are separate trust boundaries and must not be collapsed merely because they run on one host.

### Recovery is part of correctness

A feature is not operationally complete if its durable state cannot be classified, backed up when necessary, and restored or regenerated predictably.

### Do not overbuild the first release

The architecture must allow rich contextual scenes later, but V0 should first prove a reliable, secure, installable, diagnosable, and recoverable media appliance.

## 3. Technology direction

Initial application direction:

```text
Python
Flask
SQLAlchemy
SQLite
Alembic (direct integration with a thin Flask CLI adapter)
Flask-Login
Flask-WTF
Pillow
requests                 # when HTTP/provider work becomes active
feedparser               # when RSS/Atom work becomes active
```

Runtime/system components:

```text
Chromium
mpv
systemd
SMB / NFS mounts
Linux graphics stack selected by #31
HDMI-CEC tools
DDC/CI tools
Linux DRM/KMS display control
```

### Bootstrap package and tooling

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

### Why Flask

PostcardScene needs a proper web settings/control surface, authentication, forms, persistence, and APIs, but the web layer is not the main application runtime.

Flask plus a small set of mature libraries provides the required control-plane capabilities without making the rest of the application conform to a larger full-stack framework.

The project should not become a collection of thin Flask extensions. Ordinary Python should be used where framework integration is unnecessary.

### Why SQLite initially

Users, settings, schedules, source definitions, scene definitions, media catalog records, and other application state do not initially require a separate database server.

SQLite keeps installation and recovery simple. Original media remains in filesystems or external services rather than in the database.

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

## 4. High-level architecture

```text
                              PostcardScene
                                   |
              +--------------------+--------------------+
              |                                         |
        CONTROL PLANE                         LONG-RUNNING RUNTIME
              |                                         |
            Flask                               Runtime/player service
              |                                         |
      Auth / Settings / UI                    Scene / sequence engine
      Status / Control API                    Operating scheduler
      Configuration                            Background jobs
              |                               Renderer supervision
              |                                         |
              +-------------+---------------------------+
                            |
                     SQLite / state
                            |
              +-------------+---------------------------+
              |                                         |
      Source definitions                         Media catalog
                                                        |
                                           filesystem MediaItems
                                                        |
                              +-------------------------+------------------+
                              |                         |                  |
                         Chromium                     mpv          Display power
                      rich/image/web scenes        video/audio      CEC/DDC/DRM
                              |                         |
                              +------------+------------+
                                           |
                                  Linux graphics session
                                           |
                                      HDMI display
```

The web/control and runtime/player processes must be independently restartable.

A Chromium, mpv, cataloging, source, or media failure must be recoverable without losing access to the web administration interface. Restarting the control plane should not unnecessarily destroy the active display session.

The precise control-plane/runtime IPC mechanism remains intentionally open.

Issue #17 provides the packaged `postcardscene-runtime` executable and ordinary
Python `RuntimeHost`, independent of Flask and display hardware. #52 injects a
shared Database/PathPolicy and starts one catalog request thread; the executable
checks the explicitly migrated database before starting. A bare host remains
available for lifecycle tests. It starts no renderers or schedules. `host.status` returns a frozen
`RuntimeStatus` with lifecycle state and fixed safe summary; control callers can
inspect it and call `request_shutdown()`. No transport or serialization is added.

Lifecycle states are `starting`, `running`, `degraded`, `stopping`, `stopped`, and
`error`. A host runs once: normal execution advances from starting through running,
stopping and stopped. Host-thread `mark_degraded()` represents optional impairment
while remaining alive; fatal failures set error, request cancellation and propagate.
The executable returns nonzero on fatal failure without publishing raw exceptions.
Physical panel sleep/off remains separate: runtime stays running or degraded.

One host-owned `threading.Event` is the process cancellation boundary. SIGTERM and
SIGINT request the same shutdown path; the host waits interruptibly on that event.
Future source reconciliation, scheduling, provider refresh and renderer supervision
belong here, outside Flask requests, and must observe the shared event without
clearing it and bound their work and cleanup. This freezes ownership/cancellation,
not a general concurrency framework. #52 uses one dedicated catalog thread with
a bounded join; no job framework or third service is added.
Managed systemd installation remains owned by #11/#26.

## 5. Core composition model

The foundational composition model is:

```text
Source -> Widget -> Scene -> Sequence
```

`ScenePlacement` and `SequenceMembership` are owned relationship/occurrence
records, not additional universal composition concepts. None of the four models
persists runtime-selected MediaItem identities; #30 owns the later Source catalog.

### Source

A `Source` defines authority for content or data.

Examples:

- local directory
- mounted SMB/NFS directory
- Immich
- Wayfarer
- weather provider
- RSS/Atom feed
- generic HTTP/JSON endpoint
- arbitrary supported web URL

Sources should expose normalized data to the rest of the system where useful instead of forcing scenes to understand provider-specific mechanics.

### Widget

A `Widget` presents one kind of media or information.

Potential widget types include:

- image
- portrait image pair
- video
- web view
- map
- weather
- current location/place
- headline/news list
- photo strip
- text
- clock/time context

Widgets consume source data, catalog entries, and/or shared scene context.

### Source and Widget persistence

Issue #18 adds `postcardscene.domain` and explicit migration `0004_source_widget`.
Both models inherit the common Base and store generated integer IDs, trimmed
1–128 character names, ordinary 64-character string kind columns, JSON-object
configuration and boolean enabled state. Application allowlists own Source kinds
`local_directory`, `mounted_directory`, `web_url` and Widget kinds `image`,
`portrait_image_pair`, `video`, `web_view`; adding kinds needs no enum migration.

A Widget has at most one nullable `source_id`. Independent inputs belong primarily
in separate Widgets composed by a Scene. The foreign key restricts Source deletion;
the domain reports a referenced Source explicitly. Widget deletion never deletes
its Source. Disable preserves both records and never changes the other's flag.
Scene references restrict Widget deletion as defined below. Deliberate removal
proceeds Sequence -> Scene -> Widget -> Source; only owned membership/placement
rows cascade. Disabled objects remain structurally valid references at every layer.

Ordinary application writes use the explicit create/update/remove functions in
`postcardscene.domain` inside `Database.transaction()`; get/list functions return
session-bound ORM records. Creates flush to obtain identity; updates validate all
fields before assignment and replace the entire configuration object. Do not use
direct ORM writes or in-place JSON edits as an application mutation API. Capture
IDs/values within the transaction because normal commit expiration still applies.
Validation errors are `DomainError`; storage/constraint failures retain the shared
SQLAlchemy rollback contract.

The framework-independent structural validator accepts only dicts with string
keys and finite JSON-native values, copies validated input, and bounds compact
UTF-8 JSON to 16 KiB. This is not provider semantic validation: #22 owns paths,
#7/#29 URLs/security, and later providers their own semantics. Configuration must
not contain credentials before #29 establishes credential-at-rest handling, nor
original media, catalog metadata, derived provider payloads or runtime selections.
No catalog is added here; #30 attaches filesystem MediaItems beneath Source IDs.

### Scene

A `Scene` is the primary visual composition shown on the display.

A scene can be simple:

```text
+------------------------------------------+
|                                          |
|                 PHOTO                    |
|                                          |
+------------------------------------------+
```

or composed:

```text
+------------------------------------------+
|                                          |
|              WAYFARER MAP                |
|                                          |
|                          +-------------+ |
|                          | 27 C        | |
|                          | Kandy       | |
|                          | Cloudy      | |
|                          +-------------+ |
|                                          |
| [photo] [photo] [photo]                  |
+------------------------------------------+
```

The rich composition above is a later capability. Issue #19 freezes V0 persistence
in `postcardscene.domain`, through migration `0005_scene` after
`0004_source_widget`, preserving administrator/settings/Source/Widget state.
`Scene` stores a generated integer ID, trimmed 1–128 character name, ordinary
bounded string layout, nullable `duration_seconds`, and boolean enabled state.
Duration is `None` (no fixed Scene dwell) or an integer 1–86400; booleans are
rejected. Runtime/content completion, timers, Sequence overrides (#20), and
operating hours (#9) remain separate concerns.

The persisted layout vocabulary and canonical region positions are:

| Layout | Regions in position order (starting at 0) |
| --- | --- |
| `single` | `main` |
| `split_vertical` | `left`, `right` |
| `split_horizontal` | `top`, `bottom` |

`ScenePlacement` owns a generated integer ID, `scene_id`, `widget_id`, position,
and bounded region string. Every required region appears exactly once, with
canonical contiguous positions derived by the domain. A Widget can be reused
across Scenes but only once within each Scene. Any current Widget kind may occupy
any region, including disabled Widgets. A portrait pair is one
`portrait_image_pair` Widget in a `single` Scene's `main` region; Scene has no
second pairing mechanism or runtime-selected media identities.

`create_scene` and `update_scene` accept a complete list of `(region, widget_id)`
pairs in any order. Update replaces all Scene fields and placements. All domain
validation, including Widget existence, precedes mutation, so catching a
`DomainError` inside a committing transaction preserves the prior Scene and
placement identities. `get_scene`, `list_scenes`, `list_scene_placements`, and
`remove_scene` complete the ordinary Python seam; placement reads order by position.
Use the existing `Database.transaction()` with no Flask initialization.

Widget deletion fails clearly while referenced, backed by a restrictive FK.
Deleting a Scene cascades only to its owned placements; replacement never deletes
Widgets or Sources. Disabling either object preserves the other's state and all
references. Sequence memberships restrict Scene deletion as defined below.

No generic Scene JSON, coordinates, nested layouts, rendering, selected MediaItem
IDs, or speculative panel-safety fields are persisted. #8/#10 must establish an
exact semantic need before adding safety state; never infer it from name/layout.

### Sequence

Issue #20 persists Sequence configuration through `postcardscene.domain` and
migration `0006_sequence`, preserving existing foundation/domain state. A Sequence
has a generated integer ID, trimmed 1–128 character name, ordinary bounded string
`mode` (`ordered` or `shuffle`), and boolean `enabled`.

`SequenceMembership` represents one Scene occurrence with a generated integer ID,
`sequence_id`, `scene_id`, zero-based `position`, and nullable
`duration_override_seconds`. Complete nonempty lists of `(scene_id, duration)`
pairs determine contiguous positions; queries return rows in position order.
The database enforces unique `(sequence_id, position)`. Duplicate Scene IDs are
allowed within and across Sequences; each occurrence has its own identity.
Configured order remains authoritative in both modes. No randomization occurs here.

Duration override is `None` to defer to Scene duration/later content completion,
or an integer 1–86400 (never bool). It never changes Scene duration. This is the
only membership timing field. Disabled Scenes are valid references, including an
all-disabled set. Disabling a Scene or Sequence preserves memberships and never
changes the other object's enabled flag. Missing Scenes are rejected.

Create/get/list/update/remove and `list_sequence_memberships` use the existing
short `Database.transaction()` seam without Flask. Updates validate all proposed
fields and occurrences before mutation, so a caught `DomainError` cannot partially
replace configuration. Updates replace the complete membership set; occurrence IDs
remain stable while stored configuration remains in place, but deliberate list
replacement may assign new IDs. No identity reconciliation is required.

Referenced Scenes cannot be deleted through either the domain or restrictive
membership FK. Deleting a Sequence cascades only to its owned memberships;
replacing memberships never deletes Scenes, Widgets, or Sources.

#8 owns execution, eligibility/fallback, shuffle, cursor and history. #9 owns
operating schedules/timezone/DST. No playback state, random seed, transitions,
weights, probabilities, conditional rules, or generic Sequence JSON is persisted.

## 6. Filesystem media catalog

The media catalog is a source/runtime data layer, not a fifth universal composition concept.

Conceptually:

```text
Filesystem Source
      |
      v
bounded enumeration
      |
      v
MediaItem catalog
      |
      v
Widget selection
```

`MediaItem` persists Source-relative identity, type, size/mtime freshness, scan
generation and image presentation dimensions/orientation/status. Integer row IDs
serve storage/pagination, not stronger identity across removal/reappearance.
Video `duration_ms` remains nullable and unpopulated; active playback supplies duration;
capture timestamps, media bytes, thumbnails and broad EXIF are not indexed.
Original image/video bytes remain in the source filesystem.

### Filesystem Source contract (#22)

`postcardscene.filesystem_source` owns ordinary-Python semantic validation for
both existing `local_directory` and `mounted_directory` Source kinds. Their whole
configuration is exactly `{"path": "/absolute/media/path", "recursive": true}`.
`validate_source(kind, configuration, policy)` returns normalized JSON. It requires
a real boolean and a nonempty absolute Linux path, rejects extra/missing keys,
NUL, `..`, tilde, shell/environment/glob/URI syntax, and normalizes redundant `/`
and `.` components. Case and Unicode remain unchanged. Nonrecursive sources admit
only direct regular-file children; recursive sources may traverse normal directories.

`PathPolicy(allowed_roots)` comes exclusively from trusted host/application
configuration. There are no default roots, Source JSON policy fields, database
policy table, or ordinary UI authority to widen it. `/` is forbidden, including
canonical aliases of `/`. #25 uses the same validator; #26 owns installation
provisioning. Generic domain `create_source`/`update_source` still validate JSON
structure only: operational adapters and Source forms must invoke semantic
validation before filesystem use/persistence. Existing Source persistence and
`SCHEMA_REVISION = 0006_sequence` remain unchanged.

Both lexical containment and canonical containment beneath the corresponding
allowed root are required. Existing ancestor symlink escapes fail closed even
when the leaf is missing. Missing/unreadable storage does not invalidate semantic
configuration; `source_status` returns only `available`, `unavailable`, or `invalid`
without raw error text. An accessible non-directory is `unavailable`. Availability
requires a readable/searchable directory within authority. A Source-root alias
within allowed canonical authority is permitted; descendant symlinks are not.
Host policy roots and their ancestors must remain under trusted host control.

`resolve_item_path` rechecks current Source authority and every relative component:
no symlink files/directories, no special files, only normal intermediate directories
and a regular-file target. Its returned Path is a point-in-time check, not an open
file capability: consumers must recheck at use and protect actual opens against
concurrent replacement; never cache a checked absolute path as permanent authority.
#23/#24 must likewise never follow or emit symlink descendants.

Identity is `(Source ID, canonical source-relative path)`, using `/` separators,
no absolute/empty/`.`/`..` components, and unchanged case/Unicode. Inodes/devices are
not durable identity. Hard links at different paths are distinct occurrences;
rename means removed old path plus added new path. The frozen `MediaEntry` carries
only `relative_path`, broad `MediaType` (`image`/`video`), nonnegative integer
`size_bytes`, and integer `mtime_ns` from `st_mtime_ns` (including pre-epoch times).
#23 owns conservative candidate extensions; discovery is not proven playback
support. #30 owns further metadata/indexing; no decoder or catalog is added here.

Concrete enumerators yield entries incrementally. **Only normal iterator exhaustion
means authoritative completion.** `InvalidSource`, `SourceUnavailable`,
`ScanCancelled`, and `EnumerationFailed` all derive from `FilesystemSourceError`
and mean incomplete traversal: #30 must never delete unseen entries on that basis.
#23/#24 observe #17's stop event (or its `is_set` predicate) between filesystem
operations/yields and raise `ScanCancelled`; cancellation must not be swallowed or
converted to normal exhaustion. No database transaction spans traversal. This
issue implements no scan loop or durable scan state.

Synchronous path probes can block inside a kernel filesystem call. Cooperative
cancellation cannot interrupt every NFS/SMB syscall. #24 owns isolation/time bounds
and mount-outage detection, including preventing fallback to a local directory
under a missing mount; an available directory alone is not proof of a live mount.
Use the isolated mounted adapter below for mounted probes and enumeration; the
synchronous #22 helpers themselves provide no mounted-operation time bound.

### Local directory enumeration (#23)

`enumerate_local_directory(configuration, policy, *, cancelled=None)` in
`postcardscene.filesystem_source` validates/resolves through the #22 helpers and
yields one immutable `MediaEntry` at a time. It needs only ordinary Python and
local filesystem access. Consumers that stop early must close the iterator to
release active directory descriptors; an abandoned scan is not complete.

With `recursive=false`, only direct children are inspected. With `recursive=true`,
normal directories are traversed depth-first. Each directory's immediate names
are sorted by exact Linux filename string, preserving case and Unicode without
normalization. Hidden names are included normally. Memory holds active directory
listings along the descent, never a collected/sorted library of media entries.
Directory opens and descriptor-relative file stats use no-follow semantics;
symlink descendants and special files are skipped. File bodies are never read.

Candidate suffix matching is case-insensitive, preserving the original relative
path. Image suffixes are `.jpg`, `.jpeg`, `.png`, `.webp`, `.gif`, `.bmp`, `.tif`,
`.tiff`, `.heic`, `.heif`, `.avif`; video suffixes are `.mp4`, `.m4v`, `.mov`, `.mkv`,
`.webm`, `.avi`, `.mpg`, `.mpeg`, `.mts`, `.m2ts`. Unknown suffixes, including camera
RAW formats, are ignored. This is discovery, not a renderer/platform support
promise. Size and nanosecond modification time come from the no-follow stat;
metadata decoding remains #30's responsibility.

Cancellation accepts a predicate such as `stop_event.is_set`, checked before
traversal, around listings, while processing children, before descent and before
yields. It raises `ScanCancelled` immediately at the next check. Root storage
unavailability raises `SourceUnavailable`; invalid configuration/authority raises
`InvalidSource`. Child/subtree operational failures permit safely reachable sibling
results, but finish with `EnumerationFailed`. Expected skips do not invalidate a
scan. Only normal exhaustion permits #30's absence-based reconciliation. No DB
transaction or runtime registration is part of enumeration, and schema revision
remains `0006_sequence`. Mounted sources use the isolated wrapper below.

### Mounted directory isolation (#24)

`postcardscene.mounted_source` exposes `mounted_source_status(configuration,
policy, *, timeout_seconds=10.0)` and `enumerate_mounted_directory(configuration,
policy, *, cancelled=None, timeout_seconds=10.0)`. Timeout is trusted call policy,
a finite positive number, never Source JSON. Configuration remains exactly
`path` and `recursive`; schema remains `0006_sequence`.

Each probe/scan owns a disposable `multiprocessing` **spawn** child. All mounted
path validation, resolution, directory access and shared #23 traversal run there.
The parent checks cancellation while waiting on a bounded OS Pipe; it does no
mounted-path filesystem work. Entries and small traversal-progress messages reset
the idle timeout, so unsupported files/directories also count as progress and a
healthy scan may exceed ten seconds overall. Consumer processing time does not
count against worker progress. No library-wide result list is built.

Inside the child, the resolved Source must be within the deepest component-covering
mount in `/proc/self/mountinfo`, whose type must be `nfs`, `nfs4` or `cifs`.
Linux whitespace/backslash escapes are decoded. Subdirectories within mounts are
allowed; a deeper local mount overrides a network ancestor. A readable local
mountpoint without network coverage is unavailable. The worker rechecks authority,
readability and network coverage before explicit success, including after traversal.
Mount server/export identity is not persisted; a different qualifying share at the
same path is outside this detection guarantee. Linux mounting remains operator-owned.

Invalid authority/configuration raises `InvalidSource`; not-mounted, unreadable,
missing or idle-timed-out storage raises `SourceUnavailable`. Child crashes,
protocol errors and shared traversal uncertainty raise `EnumerationFailed`.
Status maps these to `invalid` or `unavailable`, and only success to `available`,
without child diagnostics. Only explicit worker success permits normal iterator
exhaustion. Entries preceding a failure remain observations, never authority to
delete unseen catalog rows. Every subsequent call starts fresh, enabling recovery
without application restart or sticky health state.

The parent polls the caller cancellation predicate while waiting and raises
`ScanCancelled` promptly. Timeout, cancellation, error and iterator close terminate
and join the child, escalating to kill after a short bounded join. Consumers must
close an iterator they stop consuming (for example with `contextlib.closing`).
Linux cannot guarantee immediate reaping of a task in uninterruptible kernel sleep;
if it survives both bounded joins, cleanup reports failure rather than success.
Tests exercise spawn, blocked workers and kill escalation with controlled local
substitutes; they do not claim physical NAS/kernel recovery evidence.

This is an operation boundary only: no RuntimeHost service, retry loop, scheduler,
Flask scan, mount command, credentials, catalog state or migration is introduced.
Later runtime work owns scan timing and retry decisions and must hold no database
transaction across the operation.

### Persistent catalog and reconciliation (#30)

`postcardscene.catalog` owns `MediaItem` and per-Source `MediaCatalogState` in the
existing application SQLite database. Explicit migration `0007_media_catalog`
preserves administrator/settings and all composition configuration. Source deletion
cascades its derived rows/state; disable preserves them and reconciliation rejects
it without mutation. Source-to-Widget deletion restrictions remain unchanged.

`catalog_reconciliation.reconcile_filesystem_source(database, source_id, policy,
*, cancelled=None, batch_size=100, metadata_batch_size=32,
mounted_timeout_seconds=10.0)` performs one operation outside Flask requests.
#52 serializes explicit requests through this operation on one runtime thread;
#30 adds no scheduling, service or queue. Presence batches accept 1–500 entries;
metadata batches accept 1–64. All filesystem work and mounted waits occur outside
short database transactions, consuming exactly the #23/#24 enumeration stream.

Every observation carries the current per-Source generation. Only normal enumerator
exhaustion authorizes deletion of unseen rows, in bounded ID batches. Partial,
unavailable, invalid or cancelled enumeration preserves unseen rows. Batch writes,
cleanup and metadata commits verify the generation; superseded attempts stop with
`ScanSuperseded` and cannot overwrite newer state. Runtime still owns serialization.
Handled presence results are `never_scanned`, `ready`, `unavailable`, `error`,
`cancelled`; `scan_generation != completed_generation` signals interruption.
`last_attempt_ns` and `last_success_ns` are UTC Unix epoch nanoseconds. Typed scan
failures propagate after recording state where possible.

After authoritative presence completion, pending/error images are queried in bounded
pages. Pillow reads headers and header EXIF only, swapping presentation dimensions
for orientations 5/6/7/8 before portrait/landscape/square classification. PNG trailing
EXIF is not sought by decoding pixels. Ready unchanged images are not reopened;
changed freshness clears derived fields. Bad images retain presence with metadata
`error`; freshness races leave metadata pending for a later reconciliation. Reads
use #22 safe resolution and recheck size/mtime afterward, retaining its documented
point-in-time race limitation rather than claiming an atomic filesystem snapshot.

Mounted image batches reuse #24's disposable spawn operation, mount guard, streamed
per-item results/progress, idle timeout and bounded cleanup. The parent never opens
mounted images. Metadata timeout/cancellation/worker failure propagates while leaving
the successful presence result `ready`; unresolved images remain retryable. Metadata
failure never changes presence authority or blocks confirmed-removal cleanup.

Within a short `Database.transaction()`, `get_media_item` looks up canonical
`(source_id, relative_path)` identity; `list_media_items` uses bounded `limit` and
`after_id` pagination with Source/type/ready-image-orientation filters. Counts and
`catalog_health_counts` provide bounded result summaries. There is no full-library
listing default, shuffle/history policy, or unrestricted persisted absolute path.

Catalog rows/state are **regenerable derived state** for #27. Whole-database backups
may incidentally include them, but restore must require fresh reconciliation before
treating catalog freshness as authoritative. Original media remain external/unowned;
no second catalog database or catalog-dependent recovery authority is introduced.

### Manual catalog refresh requests (#52)

`0008_catalog_requests` preserves existing application/catalog state and adds
nonnegative integer `requested_generation` and `handled_request_generation`
counters, both defaulting to zero. A missing state row or zero requested generation
means no refresh has been requested; `refresh_pending` means requested > handled.
Together with #30's scan/completed mismatch, result and attempt/success timestamps,
this exposes queued, active/interrupted and last handled health without per-file
progress. These are coalescing tokens, not a job/event queue or distributed lock.

`catalog_requests.request_catalog_reconciliation(database, source_id)` validates
an existing enabled filesystem Source and increments its token in one short DB-only
transaction. It neither probes paths nor imports/calls reconciliation. The #25
authenticated Sources UI uses this seam without scanning in a Flask request.

The runtime-owned `CatalogRefreshWorker` polls the first pending Source by ID with
`LIMIT 1`, serially invoking #30 off the RuntimeHost thread. It captures request N;
#30 checks N against supersession in its existing start transaction before capturing
Source authority. Success/unavailable/error consumes only N, leaving N+1 pending.
No automatic failure retry occurs. Shutdown cancellation leaves N pending for the
next runtime start; deletion/disable safely skips obsolete work. A database or
infrastructure failure stops the host with error rather than silently losing the
worker. Polling waits one second between attempts, including failures and idle polls.

`domain.update_source` calls the focused `catalog.supersede_catalog` helper in the
same transaction as an authority change (kind/path/recursive) or disable. Authority
changes increment the scan token, consume outstanding requests, delete derived
MediaItems and reset result/timestamps to never-scanned. Tokens remain monotonic;
the state row is not recreated. Disable supersedes scans/requests but preserves
items, result and successful knowledge. Re-enable/rename does not queue work; #25
requests a fresh token after an enabled authority edit. Source deletion retains
composition restrictions and cascades catalog/request state.

Short catalog writes and Source edits use `Database.transaction(write=True)`:
`BEGIN IMMEDIATE` reserves SQLite's single writer before read/modify/write, retaining
the existing bounded busy timeout. Read-only transactions still use `BEGIN`. No
transaction spans filesystem work, mounted IPC or cancellation waits; this is
SQLite transaction policy, not a lease or advisory lock.

Both entrypoints load the same optional trusted executable Python configuration
file selected by `POSTCARDSCENE_CONFIG` through `configuration.load_operator_config`.
The runtime consumes only `DATABASE_PATH` and `MEDIA_ALLOWED_ROOTS`, without importing
Flask. Missing/unreadable configured files fail startup. `MEDIA_ALLOWED_ROOTS` is a
list/tuple of absolute host roots consumed through #22 `PathPolicy`, empty by default,
never persisted in Source JSON or editable through the ordinary UI. #25 reads the
same key from Flask configuration; #26 later owns installation provisioning.

Shutdown sets the shared stop event and joins for at most five seconds. Cooperative
cleanup leaves no live catalog thread. Mounted scans/metadata retain #24 isolation;
local kernel calls retain their synchronous-call limitation. If a local call blocks
past the join bound, shutdown reports error; the thread is daemonic so it cannot
prevent process exit. This does not claim Python can cancel an arbitrary syscall.
#52 adds no automatic cadence, scheduler, third service, pool or playback.

## 7. Image behavior

### Image Widget semantics and selection (#56)

`postcardscene.image_selection` is the ordinary-Python, DB-only semantic seam for
`image` and `portrait_image_pair`. `validate_image_configuration` accepts an object
with only optional `fit`: `{}` normalizes to `{"fit": "contain"}`, and the only
values are `contain` and `cover`. Other keys/values are rejected. Generic domain
JSON validation remains structural. Presentation uses a fixed neutral black
background and centered object position: contain preserves the whole image with
unused black area; cover fills the region with centered cropping. Widget JSON
contains no duration, transition, selection/shuffle/history, crop coordinates,
color-profile or panel-safety configuration.

`resolve_image_context(session, widget_id)` requires an existing enabled image
Widget referencing an existing enabled `local_directory` or `mounted_directory`
Source. Invalid contexts raise `ImageSelectionError` without catalog mutation.
Within the caller's short transaction, `get_selected_image` looks up canonical
`(source_id, relative_path)` under that Widget, returning `None` for absent,
mismatched or ineligible media. `list_selected_images` returns an immutable page
plus its last row ID as continuation, or `((), None)` at exhaustion. Both queries
resolve context on each call. Only same-Source images with metadata `ready`,
positive presentation dimensions and a normalized portrait/landscape/square
orientation are eligible; video and pending/error metadata are excluded.

Pages accept 1–500 items and a nonnegative `after_id`. The existing
Source/type/orientation index supports three bounded orientation queries; merging
at most three pages avoids sorting a whole remaining Source catalog. Row IDs are
only keyset continuation details, never media identity or playback ordering.
The schema and indexes remain unchanged at `0008_catalog_requests`.

Frozen `SelectedImage` records contain only Source ID, canonical relative path,
expected `size_bytes`/`mtime_ns`, presentation width/height and normalized
orientation. Frozen `ImageFrame` records hold one or two selected images plus
normalized fit. Neither retains an ORM object/session, absolute path, bytes,
Scene identity or playback state. Later file delivery must re-resolve current
Source authority and recheck freshness when opening; a snapshot grants no lasting
filesystem authority. Selection performs no storage probe, scan, read or decode.

Catalog/Pillow metadata readiness is **not proof of Chromium format support**.
There is no browser-format extension filter in selection. #57 owns browser
readiness/decode-failure handling, #58 integrates that result with the real #31
controller, and #59 owns the physically evidenced format/color/quality matrix.
An eligible image may fail browser decoding safely without catalog deletion.

### Automatic portrait grouping and runtime ownership

`build_image_frame(widget_kind, fit, first, lookahead=None)` returns
`(frame, consumed_count)`. The caller supplies already-selected candidates in its
own runtime order. Ordinary `image` always consumes one. `portrait_image_pair`
pairs only two distinct canonical identities with portrait orientation and
consumes two; otherwise it presents the first singly and consumes one. Landscape,
square and final unpaired portraits remain visible. Duplicate identity never
pairs, even when freshness differs. Lookahead remains caller-owned and unconsumed
on single fallback. There are no aspect-ratio heuristics or image-owned iterator,
push-back buffer, cursor, shuffle or history.

#8 defines consecutive candidates through its ordered/shuffled stream and advances
by the consumption count. It owns display-step history, dwell execution,
Previous/Next, Play/Pause and common auto-hiding controls. Scene duration and
Sequence overrides remain #19/#20 configuration. Shared transitions remain #8;
#57 may use a simple fixed image-local fade without another Widget option.
#10 owns static-dwell/panel protection, including while paused.

### Trusted image frame delivery (#57)

`capture_image_source(session, frame)` captures a frozen DB-only `ImageSource`
for a validated one/two-image `ImageFrame` and one enabled filesystem Source.
Capture immediately before `start_image_frame(frame, source, policy)`; the snapshot
is short-lived launch authority, not a live subscription to Source edits. No ORM
object or Source-path I/O crosses into the parent delivery operation.

Each frame owns one disposable `spawn` helper bound to `127.0.0.1` on an ephemeral
port. Its unpredictable token authorizes only the fixed page, asset `0` (and `1`
for a pair), and empty ready/failed POSTs. Exact routes, Host/origin checks and
hashed-inline CSP restrict browser authority; there is no general media API,
Flask service, `file:` access, persistent cache or playback-control surface.

All local/mounted Source access occurs inside the helper. `open_image_item`
rechecks #22 authority, pins directories with descriptor-relative no-follow opens,
rejects special files, and compares opened size/mtime to selection before serving.
Mounted access also reuses #24's deepest NFS/NFS4/CIFS mount guard and checks the
opened descriptor's mount identity to refuse local mountpoint fallthrough.
Original compressed/profile bytes stream from that descriptor in bounded 64 KiB
chunks with HTTP backpressure, without Pillow decoding or conversion.

The fixed black, centered HTML page uses one viewport image or two equal vertical
columns, `object-fit: contain|cover`, and `image-orientation: from-image`. It waits
for all image decodes before asserting ready, or asserts failed on load/decode
failure. The first valid terminal callback wins; byte transfer alone is not ready.
`handle.wait_ready()` consumes the closed progress/terminal protocol and reaps the
helper. Always close the handle (or use it as a context manager). Startup and
waiting enforce a trusted finite positive lack-of-progress timeout and prompt
caller cancellation. Close uses bounded terminate/join/kill/join; Linux
uninterruptible kernel sleep can still prevent timely final reaping.

`ImageDeliveryError.reason` distinguishes invalid context, unsafe/stale item,
unavailable Source, timeout, helper/protocol failure, presentation failure and
cancellation without raw diagnostics or catalog mutation. #8 owns skip/fallback
and global controls. Controlled HTTP tests prove this protocol only: #58 must
prove real supervised Chromium navigation/readiness and frame retention after
helper exit; #59 owns actual format/color/quality evidence.

### Presentation and quality boundaries

Chromium/HTML/CSS is the V0 image compositor selected by #5; mpv remains the video
engine. #57 implements trusted disposable image delivery and the HTML/CSS frame; #31
owns the production graphics session, Chromium supervision and common overlay/input
mechanism; #58 adapts image frames to that controller. No renderer is added by #56.

Image presentation must respect EXIF orientation, preserve original bytes/profile
data and useful 1080p/validated-4K quality without ordinary recompression, and bound
malformed, huge, unreachable or unsupported-image load/decode failures. #59 must
document actual tested formats, color behavior and physical quality limitations
before claiming supported playback. No speculative user-facing playback guide is
created ahead of that evidence.

## 8. Video and audio behavior

Issue #71 implements `postcardscene.video_selection`, an ordinary-Python,
DB-only semantic/query seam. Generic `domain.create_widget` JSON validation stays
structural. `validate_video_configuration` accepts only kind `video` and an object
with optional `audio_enabled` (real boolean, default `false`) and `volume`
(integer 0–100, default 50; booleans rejected). Unknown keys are invalid. Volume
may remain configured while audio is disabled without implying sound. Mute is
transient active-player state; intended audio-device selection is a trusted
host/runtime concern, not Widget JSON. Duration, seek increments, codec/hwdec
policy, Scene dwell, Sequence state and panel safety are not Widget options.

`resolve_video_context` requires an enabled video Widget referencing an existing
enabled local or mounted filesystem Source. `get_selected_video` looks up canonical
`(source_id, relative_path)` identity; `list_selected_videos` returns a tuple of
snapshots and a row-ID keyset continuation, with default 100 and maximum 500 items.
Both use the common catalog, exclude other Sources and non-video items, and need
no image metadata or `duration_ms`. Missing/disabled/invalid context raises
`VideoSelectionError`; absent/ineligible media returns `None`. Malformed paths
retain the shared filesystem validator's typed `InvalidSource` error; pagination
bounds raise `ValueError` as in the catalog.

Immutable `SelectedVideo` carries only `source_id`, canonical `relative_path`,
nonnegative integer `size_bytes` and integer `mtime_ns` (including pre-epoch values).
It carries no ORM/session, absolute path, file descriptor, bytes or playback state.
Selection does no filesystem scan/open, reconciliation, decoder probe or catalog
mutation. #72 revalidates and pins current file authority as described below.
Catalog video classification identifies candidates, not actual mpv/container/codec
support; unsupported media remains catalog knowledge. Progress/duration comes
from the active player without catalog-wide duration indexing.

#8 owns order/shuffle/history, Previous/Next, global Play/Pause and transport UI;
row-ID pagination here defines none of those behaviors. #72–#75 own file authority,
mpv and active-player controls/audio. End-user playback is not implemented by #71;
physical format/HDMI/hwdec claims remain gated by #66/#76.

### Pinned video file authority (#72)

Call `video_file.capture_video_source(session, selected)` in a short transaction
immediately before opening; it revalidates the current enabled filesystem Source
and copies its identity/configuration without storage I/O. End the transaction,
then use `with open_video_item(selected, source, policy) as fd:`.
The immutable #71 selection is freshness state, not file authority. Capture is
point-in-time; callers must recapture for every new pin after Source changes.

The context yields one borrowed read-only, non-inheritable-by-default integer FD.
It checks Source identity, canonical relative path, trusted PathPolicy authority,
recursive policy, no-follow descendants and regular-file size/mtime freshness.
The existing image stream API retains the same shared opener and mount checks.
Rename/replacement after pin cannot redirect the FD; in-place writes by an external
owner are not prevented, and equal-size/equal-mtime content changes cannot be
distinguished by the catalog freshness contract. Original media remains
external/unowned and is never copied into PostcardScene.

Mounted opens run entirely in a fresh spawned helper, including path resolution,
open/fstat, current network coverage and the pinned FD's actual mount identity.
A private Unix socket transfers the already-open FD using SCM_RIGHTS, never a
pathname for later reopening. Progress resets the configurable ten-second default
idle timeout. Cancellation is polled during waiting. Every outcome closes socket
and partial FD copies and uses bounded terminate/join/kill/join cleanup; a kernel
task that resists reaping reports helper failure, preserving an existing primary
failure. No mount worker/service or fallback to local mountpoint content is added.
Local opens remain synchronous with cooperative cancellation between operations.

`VideoFileError.reason` is only `invalid`, `unavailable`, `timeout`,
`cancelled` or `helper`; diagnostics omit paths and raw OS/child details.
The FD is trusted player plumbing only: do not close the borrowed FD, publish it
in Flask/status, or persist it. The #73 controller duplicates this borrowed FD
during preparation, so the safe-open context can close independently. No player
is launched by #72, and physical NAS/codec/HDMI support remains unproven.

### Supervised video process (#73)

`video_player.MpvController` consumes the same Wayland session and implements the
`ContentSurfaces` VIDEO owner contract. Construct the coordinator first, then
`prepare(fd)` with a borrowed #72 pinned descriptor. Preparation duplicates the
read-only regular-file FD; callers may then exit the safe-open context. Only one
prepared/active item is allowed. Stop clears preparation and is idempotent.

Every prepared item starts one fresh process with only the media and private
socketpair endpoint in `pass_fds`. Media uses `fd://N`; JSON IPC uses inherited
`--input-ipc-client=fd://N`, with no filesystem socket or network listener.
Launch argv is shell-free, native Wayland, fullscreen and borderless. Host config,
scripts, terminal/default input, OSC, external-file discovery and media references
are disabled. Audio follows the explicit #75 policy below. No playlist progression,
codec/hwdec selection or runtime scheduling is added.

Startup waits boundedly for `file-loaded` and a correlated fixed JSON handshake.
Messages are capped at 8 KiB and processing at 32 steps per poll. Unknown or invalid
protocol fails closed. `end-file` with reason `eof` after load means `ended`;
other endings, crash, timeout and cancellation produce fixed playback failures.
The serialized owner polls `status`/surface reconciliation regularly; there is no
background monitor or automatic retry. EOF reaps the child while retaining observable
`ended` state until owner retirement.
Stop closes parent FDs and sends bounded group TERM/KILL, retaining the child handle
and `cleanup_failed` if reaping fails. Replacement content remains blocked through
ContentSurfaces until cleanup succeeds. Launchers must retain descendants in their
process group; systemd remains the outer runtime-owner crash boundary.

### Active video transport (#74)

`MpvController.snapshot()` returns an immutable `PlaybackSnapshot`: lifecycle state,
fixed reason, cleanup marker, optional position/duration in seconds, seekability,
and absolute-seek capability. These are live private IPC properties, never catalog
`duration_ms` updates. Nonfinite, negative or missing numbers become `None`;
absolute seek requires seekability and a known positive duration. Unknown seekability
is false; an unavailable/malformed pause property fails the operation explicitly.

`pause()`/`resume()` set the actual pause boolean idempotently. `seek_relative(seconds)`
accepts finite signed deltas within ±3600 seconds; `seek_absolute(seconds)` requires
0 through the known duration inclusive. Booleans and nonnumeric values are rejected.
Out-of-range absolute targets are rejected, leaving future UI clamping to #8.
Both use mpv keyframe seek modes for ordinary playback and return a refreshed snapshot;
relative boundary behavior follows mpv, without editing-grade accuracy guarantees.

All operations share the controller lock and correlated private IPC owner. The
one-second default deadline covers each complete control operation, including lock
acquisition and snapshot refresh; callers may supply a finite timeout up to 60 seconds
and cancellation callback. Stop signals cancellation before acquiring the lock.
Timeout, cancellation and protocol/process failures during IPC retire the child through #73;
cancellation before lock acquisition sends nothing and leaves cleanup to the owner.
Retirement invalidates queued commands even if the controller is reused.
mpv command rejection raises `PlaybackError("command_failed")` without restarting.
Invalid seeks and unavailable capabilities also leave the player intact. Inactive
controls raise `not_active`, or `cancelled` after retirement; terminal snapshots remain
readable with unavailable progress/capabilities. No operation prepares another item.

Successfully loaded video has no maximum playback duration: it reaches real EOF
unless its owner retires or pauses it. These are #6 media capabilities beneath #8's
global Play/Pause. Previous/Next are #8 display-step navigation, never mpv playlist
commands. No UI/history or panel-safety policy is introduced.

### Active video audio (#75)

`prepare(fd, configuration=...)` consumes #71's validated video Widget JSON:
`audio_enabled=false`, integer `volume=50` by default, bounded 0–100. Disabled
players launch with `--audio=no` and never select an audio device. Enabled players
start unmuted with explicit initial volume, `--volume-max=100`, passthrough and
ReplayGain disabled; user config/scripts remain disabled. No normalization,
equalizer, dynamic-range processing or other advanced audio controls are added.

The controller's optional trusted `audio_device` constructor argument accepts
`None`/`auto` or one ASCII mpv `driver/device` value of at most 256 characters
(letters, digits and `_./:,=-`, no leading dash). It is a single shell-free
argument, never Widget JSON, a persistent setting, enumeration or a mixer command.
Explicit devices do not fall back to another real output: mpv's
`--audio-fallback-to-null=yes` preserves silent video on device-open failure.
Automatic selection permits mpv's normal output choice. Final HDMI/device policy
and physical support remain gated by #66/#76.

`PlaybackSnapshot.audio` is immutable: configured `enabled`, live `available`,
optional `muted`/bounded `volume`, fixed `reason`, and `device_policy` (`auto` or
`explicit`). Scalar selected-audio-track and current-output IPC properties establish
`no_track`, `device_unavailable` (including null output), or `ready`; readiness is
mpv output evidence, not proof that a physical speaker is audible. Missing output
means unavailable; malformed properties fail the bounded control operation.
Inactive/disabled/degraded audio exposes no mute/volume capability or raw device data.

`mute()`/`unmute()` set the actual boolean idempotently; `set_volume(value)` accepts
only integers 0–100, rejecting booleans and invalid values with `invalid_volume`.
These use #74's serialized deadline/cancellation/retirement boundary and refresh
live state. Unavailable audio raises `audio_unavailable`; inactive players follow
#74's `not_active`/`cancelled` contract. Adjustments write no database state and
never carry into another prepared video's initial policy.

Stop, failure, cancellation, EOF and content retirement close IPC and reap the
process group; successful mute is never the final silence authority. Failed cleanup
retains degraded ownership and reports `cleanup_failed`, not confirmed silence.
#8 must retire video before changing display steps; #10 must use this same stop
primitive for sleep/off. Neither runtime integration nor panel policy is added here.

Video playback should use mpv where practical because codec support, hardware acceleration, control, and failure isolation benefit from a dedicated player.

V0 should support:

- local/network video files through the common catalog
- full-duration playback where configured
- bounded load/start timeouts
- controlled skip/failure behavior
- supervised player recovery
- hardware decoding where the validated runtime supports it
- an honest supported codec/container boundary

V0 does not require automatic transcoding.

Audio is explicit appliance state rather than an accidental mpv default. The system must define at least:

- audio enabled/muted
- bounded configured volume when enabled
- intended audio output where practical, including HDMI on validated hardware
- safe behavior with no audio track/device
- immediate silence when the owning scene is skipped/stopped or the panel is put to sleep

No orphan audio may continue after a scene transition, player restart, or scheduled display-off state.

## 9. Scene/sequence runtime behavior

### Active playback configuration (#87)

Migration `0009_playback_settings` follows `0008_catalog_requests`, preserves
existing configuration/catalog rows, and adds two singleton settings:
`active_sequence_id = NULL` and `default_scene_dwell_seconds = 30`.
Apply it explicitly with database users stopped; startup never migrates.

The nullable active Sequence FK is the sole selection authority. `None` means
intentional safe idle, with no guessing from names, IDs, enabled state or order.
An existing disabled Sequence may be selected and remains temporarily ineligible
until re-enabled. Deleting the selected Sequence sets only the pointer to NULL
and deletes its owned memberships, preserving unrelated composition.

Ordinary Python callers use `settings.get_playback_settings`,
`set_active_sequence` and `set_default_scene_dwell`. Setters validate before
mutation in short write transactions; invalid input raises `ValueError`, while
schema/storage failures retain their separate database exceptions. Default dwell
is an integer 1–86400, never bool. No per-Widget dwell is introduced.

Progression duration precedence is membership `duration_override_seconds`, then
Scene `duration_seconds`, then application default dwell for image/portrait-pair
and web content. Untimed video uses natural EOF; explicitly timed video advances
at EOF or the dwell deadline, whichever occurs first. This normal progression
policy is distinct from #10 maximum-static-dwell/panel protection. #87 freezes
configuration semantics only; later #8 children execute timers and completion.

`playback_configuration.resolve_active_sequence(database, membership_id=...)`
returns frozen settings, Sequence and ordered membership records, optionally
including that membership's Scene and canonical Widget ID. One short read
transaction covers the entire result and closes before return. Eligibility
separates intentional idle, missing/disabled/damaged Sequence, missing/disabled
Scene, unsupported layout, and malformed occurrence/placement configuration.
A stale or foreign membership never selects another occurrence. Storage errors
propagate separately. Content-specific Widget/Source resolution stays with
#56/#71/#82; these snapshots neither select media nor authorize rendering.

V0 executes only `single` Scenes through #65's one active content surface.
A `portrait_image_pair` Widget in the `main` region satisfies side-by-side
portraits. Persisted `split_vertical` and `split_horizontal` remain valid,
round-trip configuration for V1 rich composition, but return unsupported-layout
eligibility in V0; they are never silently converted to single content.

Settings/configuration are reread at display-step boundaries, without DB
notifications or IPC. Restart creates a new transient epoch: ordered playback
later starts at the first currently eligible membership and shuffle creates a
fresh in-memory epoch. No cursor, history, random seed or current media identity
is persisted. #91 exposes active selection and dwell in authenticated Settings.

### Bounded display-step planner (#88)

`display_planner.DisplayPlanner(database, rng=...)` supplies serialized `next()`
(including automatic forward progression) and `previous()` planning calls.
`PlanResult` distinguishes ready, temporarily ineligible, history boundary and
configuration/selection failure without exposing raw exception values. Each new
forward call attempts one configured occurrence; #89 owns retries and backoff.
A successful new plan enters history when returned. The execution owner decides
when to request progression and how to handle a subsequent display failure.

Frozen `DisplayStep` records Sequence/membership/Scene/Widget identities, content
kind and exact canonical `(source_id, relative_path)` media identities only.
They carry neither file authority nor web targets. Execution must revalidate
current configuration/media before opening bytes or resolving a web target.
The planner calls no renderer and is not wired into RuntimeHost.

Ordered membership cycles preserve configured occurrences, including repeated
Scenes. Shuffle cycles visit every occurrence once and avoid repeating a cycle's
last occurrence immediately at the next cycle's start. Configuration is unchanged.
One transient media stream per Widget advances across all referencing occurrences.
`media_stream.select_candidate` uses canonical relative-path keysets and one wrap
for ordered selection. SQLite's existing Source/path uniqueness index supports
these queries without a temporary ordering tree; no schema/index is added.

Media shuffle uses an injected random fraction generated outside transactions,
then count plus ranked canonical selection in a short transaction. SQLite may
scan eligible index entries for counts/ranks; Python receives a single candidate
and at most 32 excluded paths, never a complete library. Up to 32 consumed
identities per Widget exclude recent repeats. Exhaustion relaxes oldest eligible
exclusions first, retaining the latest where alternatives exist. Small libraries
avoid reuse until exhausted; larger libraries do not promise a full permutation.

Portrait grouping uses #56's first/lookahead consumed-count contract. A rejected
lookahead remains a revalidated identity owned by the Widget stream; only consumed
candidates advance its cursor/recent window. A wrapped identical singleton is not
retained as its own lookahead.

History holds at most 32 steps, without URLs. Previous skips invalid entries
backward and stops at the oldest usable boundary. Next/automatic progression
replays valid forward history before planning new content. Replay rereads current
composition and media eligibility, requires exact historical pair members and
pair compatibility, and validates the current web target without retaining it.
Replay does not consume or rewind media streams. Source/Widget/Scene eligibility
changes may invalidate entries without resetting the epoch.

An observed active Sequence change, idle/disabled selection, membership identity
or order replacement, or mode change clears streams, cycles and history. A
concurrent epoch replacement detected during resolution returns ineligible and
starts the new epoch on the next call. Restart constructs empty transient state.
No database transaction spans RNG/history computation or future rendering.

### Playback execution worker (#89)

`runtime.playback.PlaybackWorker(database, presenter, stop_event)` owns one stdlib
thread and a maximum of 32 pending commands. `start()` is called once;
`submit(name, value=None)` returns a Future containing a fixed `Outcome`, including
busy/rejected/unavailable results. There is no executor pool or request-thread
rendering. The worker serializes planner calls, presentation, video snapshots,
transport and cleanup. #93 owns its construction in RuntimeHost and the concrete
V0 presenter; #58 remains the image prerequisite. No production renderer is
constructed by this worker capability.

`PlaybackState` consumes #88 Next/Previous directly. Planner `revalidate(step)`
reuses current composition/media validation without advancing order/history; it
supports re-presenting a held identity. Configuration is reread for each new or
replayed presentation. The presenter must re-resolve exact content authority at
execution, honor `start_paused` before video becomes audible, and implement bounded
cancellable operations. `clear` must retire all content/audio to safe black and
`stop` must finish retirement; cleanup uncertainty raises typed `cleanup_failed`.

Dwell uses injectable monotonic time with membership duration before Scene duration,
then default image/pair/web dwell or natural video EOF. Timed video ends at the
first of EOF and dwell expiry. Video state is inspected every 0.5 seconds; timed
image/web waits sleep until their deadline or a command. Global Pause freezes the
remaining budget and automatic progression, pauses actual video, and leaves image
or web content displayed (web scripts continue). Previous/Next use planner history
while retaining Pause; new video receives `start_paused=True`. Pause is transient
and is not a panel-safety override.

Navigation/state commands cancel the private in-flight operation token; cancellation
is not a failed-content attempt. Eight consecutive ordinary failed attempts retire
to degraded black/idle for five seconds before continuing the planner. A successful
presentation resets the budget. No active Sequence means intentional idle; disabled
selection also waits five seconds for reevaluation. Ineligible occurrences consume
the same bounded attempt budget. There is no configuration notification service.

`set_output_suppressed(bool)` clears content/audio and holds progression, retaining
the logical current identity and user Pause. Release revalidates/re-presents that
identity or enters normal bounded fallback. Video position restoration is not
promised. #9/#10 own suppression policy; this seam makes no physical-power claim.

Frozen thread-safe `status` contains fixed state/reason, composition IDs, remaining
dwell at the last update and optional bounded video progress/audio capabilities.
Media paths and web targets never enter it. No playback state is persisted.
Unexpected thread failure or uncertain cleanup sets a fixed `failure` and the shared
host stop event; no replacement content starts after authority loss. The owner must
call `join()` when that event wakes: join cancels/wakes the operation or dwell wait,
waits at most five seconds and raises on failed cleanup or an unresponsive worker.
The daemon flag permits fatal process exit, not clean abandonment. #93 must propagate
failure as nonzero runtime exit for systemd/cgroup cleanup; Flask remains independent.

## 10. Scene composition and rendering

Rich scenes should primarily be rendered with Chromium using HTML/CSS/JavaScript.

This provides a flexible composition surface for:

- CSS Grid/Flexbox layouts
- overlays
- cards/panels
- typography
- maps/web content
- smooth transitions
- responsive scaling between 1080p and 4K
- lightweight animations
- image presentation effects where appropriate

Chromium is the V0 image compositor selected by #5; image content consumes the
shared #31 graphics/controller boundary rather than a dedicated native stack.

The runtime/player service coordinates the active scene and Chromium/mpv processes.

## 11. Web-content isolation

Configured web pages are untrusted renderer content even when an administrator
chose the URL. #82 implements `postcardscene.web_selection` independently of Flask
and Chromium. `validate_web_source(kind, configuration)` requires `web_url` and
exactly `{"url": "..."}`. It trims outer whitespace, bounds the result to 8192
characters, rejects controls/DEL, embedded whitespace, backslashes, userinfo,
missing hostnames and invalid/zero ports, and accepts absolute HTTP/HTTPS only.
Paths, case, percent-encoding, query strings and fragments are preserved.
Loopback, private/link-local addresses and LAN/mDNS hostnames are deliberately
allowed for configured local services. Validation does no DNS, HTTP, reachability
or browser work. HTTPS retains normal Chromium certificate validation: no TLS
bypass or application-managed trust store; deliberately configured LAN HTTP is
supported when a service lacks a browser-trusted certificate.

`validate_web_configuration(kind, configuration)` requires `web_view` and exactly
`{}`. Generic domain JSON remains structural; semantic callers validate before
writes. `resolve_web_target(database, widget_id)` owns one short read transaction,
requires the current enabled Widget and enabled referenced `web_url` Source,
validates both configurations and returns a frozen `WebTarget(source_id, url)`.
Missing, disabled, mismatched or invalid state raises sanitized `WebSelectionError`.
The snapshot contains no ORM/session authority; resolve anew for every display
step and never keep a transaction open during browser/network work. URL is omitted
from the target repr; do not emit full URLs in public diagnostics or renderer logs.

Public/share-display URLs, including share identifiers in path/query, are supported
content configuration. V0 has no generic username/password/API-key/token fields,
userinfo, generated credential headers/cookies/JavaScript, cookie import/export,
profile copying, DOM login scripting, OAuth automation or authenticated-page
provisioning. Protected third-party credentials require a later #29-owned design.

Use one installation-owned `untrusted_web` Chromium profile root, separate from
trusted-image and administration cookies/secrets. Ordinary cookies, local storage
and site preferences may persist through browser restarts and host reboots within
that installation. There are no per-Source/per-site profiles. This is replaceable
browser/runtime state, excluded from V0 backup/restore authority; a replacement
host or fresh install may begin clean. Persistence promises neither third-party
login provisioning nor session portability. #64 owns profile/process lifecycle;
no profile wipe/copy machinery is added to emulate incognito.

Scene duration and Sequence overrides own dwell, executed later by #8. Each new
web presentation navigates freshly; V0 has no web-specific duration or periodic
reload option. Third-party pages may continue their own scripts/timers/network;
future Pause holds Scene progression without freezing web execution. #82 launches
no browser and adds no Scene/Sequence execution. #84's physical Pi/browser support remains gated on #31.
Production CDP stays private to local controller authority and is not a Flask API.

#83 implements synchronous `web_renderer.WebRenderer(surfaces)` with
`show(WebTarget, cancelled=...)`, `clear(cancelled=...)` and `stop()`. The caller
serializes these calls outside Flask/DB transactions. `ContentSurfaces.operation`
lends its exact untrusted controller and retains cleanup authority; it refuses
another active content class or pending retirement. #8 still owns global switching
through the coordinator. Renderer clear/stop never activate or retire other classes.

Each show validates the detached target, loads #64's fixed black document (default
five-second bound), then performs a fresh navigation with a 15-second bound.
Success means #64 main-document load completion; normal rendered HTTP error pages
remain content. There is no DOM inspection, injected control UI or background loop.
Startup/control/navigation/crash failure permits at most one fresh-browser retry,
after retirement through #64/#65. Cancellation, invalid input/configuration,
unavailable session and cleanup uncertainty are not retried. Invalid replacement
input also retires prior web content. Clear blanks; failed blank/navigation retires
to compositor black. Stop is bounded/idempotent. Cleanup failure retains authority
and prevents subsequent activation until explicit cleanup succeeds; black cannot
be guaranteed when the OS refuses retirement.

`WebRendererError` exposes only `WebFailure` (`invalid_target`, `unavailable`,
`cancelled`, `cleanup_failed`) and a secondary `cleanup_failed` boolean. It retains
no URL/target or successful-page state. #8 owns subsequent skip/fallback decisions;
#84 remains physically gated, and these software contracts claim no Pi/HDMI support.

## 12. Sources and later integrations

### Local storage

Local directories are first-class V0 media sources.

### Network storage

SMB/NFS should normally be mounted by Linux and exposed to PostcardScene as filesystem paths.

V0 does not own NAS credentials or arbitrary remote mounting. A missing/unresponsive mount is a degraded source condition, not a reason to fall back silently to an unrelated local path.

If mount management is ever exposed through the web UI, privileged work must happen through a narrow controlled mechanism rather than by giving Flask general root authority.

### Immich

Immich is a V1 adapter/source rather than a special case embedded throughout the player.

Useful data may include asset IDs, URLs/paths, media type, dimensions/orientation, capture time, geolocation, and album/source grouping.

Immich assets do not need to be forced into the V0 filesystem catalog model merely because filesystem sources use a persistent catalog.

### Wayfarer

PostcardScene should support two eventual Wayfarer modes:

1. ordinary isolated URL display
2. native structured-data integration

The native integration is expected to provide context such as live position, shared timeline state, current trip, route/timeline data, place information, and shared-location state.

### Weather, news, and generic HTTP/JSON

These are V1 integrations. Rendering should normally consume locally cached provider state rather than block on external requests.

The V0 security model for outbound URLs, credentials, logging, privacy, and retention must be extended rather than bypassed when these providers are implemented.

## 13. Shared context

Rich V1/V2 scenes need a controlled way to share context.

A key example is live travel:

```text
Wayfarer live position
          |
          v
     Shared context
          |
    +-----+------+----------------+
    |            |                |
    v            v                v
   Map        Weather          Immich
 marker      for location    nearby photos
```

The context mechanism should remain generic enough for real relationships such as this, but it should not become a speculative global event framework.

## 14. Data collection and caching

Future external rendering should normally follow:

```text
External provider
       |
       v
runtime collector / refresh job
       |
       v
local cached state
       |
       v
scene renderer
```

The long-running runtime process owns these jobs initially, just as it owns filesystem reconciliation.

Refresh intervals should be configurable where appropriate. A provider outage should leave the last valid cached state usable when sensible and should expose stale/error state without breaking unrelated scene content.

The exact general cache implementation remains open.

## 15. Live updates

Some scenes may benefit from server-to-renderer updates without full reloads, especially later Wayfarer live-location views.

Use the simplest transport that satisfies the required update cadence. Ordinary authenticated HTTP polling is preferred where it is adequate because PostcardScene is a small local appliance and the control plane does not otherwise need an async-first web architecture.

If a concrete V1/V2 requirement demonstrates that server push materially improves behavior, evaluate the smallest suitable mechanism at that time, such as SSE or WebSockets. Do not preselect Flask-Sock, Socket.IO, an ASGI migration, a message broker, or other event infrastructure without evidence that the simpler path is insufficient.

The renderer transport is distinct from the control-plane/runtime IPC boundary: live state remains authoritative in the runtime/player process, so changing browser transport must not collapse the process separation defined elsewhere in this architecture.

## 16. Scheduling and time semantics

Scheduling has two related but distinct responsibilities.

### Display operating schedule

Controls when the panel should be awake.

V0 should support:

- daily on/off periods
- multiple periods where practical
- day-specific/weekday-weekend rules
- one configured application timezone
- temporary/manual override with explicit persistence/expiry semantics

Schedule evaluation must define DST skipped/repeated local times and recover safely after reboot, host downtime, NTP correction, manual clock jumps, or timezone changes.

After long downtime, the scheduler should converge to the state that should be active now rather than replay every missed transition.

### Content scheduling

Later content rules may control which sequence/scenes are appropriate at a given time. Rich conditional scenes are primarily V2 work.

Scheduling logic belongs to the long-running application runtime, not Flask request handlers.

## 17. Linux graphics session

The live #31/#62 contract selects native Wayland with labwc for Raspberry Pi
4/5-class ARM64. Ubuntu Server LTS ARM64 on Raspberry Pi (24.04 and 26.04 where
packages are available) and Raspberry Pi OS 64-bit are both primary V0 targets.
One graphics implementation serves both; X11, alternate compositors and full
desktop/display-manager dependencies are outside V0. Windows is outside V0,
while platform-neutral application layers retain their existing boundaries.

#62 packages an isolated appliance labwc configuration and systemd/PAM templates
beneath `postcardscene.graphics`. One dedicated non-root graphical/runtime user
owns the service. The default unattended local-VT session uses pam_systemd/logind
and libseat; seatd is an explicit provisioning alternative. Distro differences
are limited to package/user/seat/device provisioning owned by #26. No managed
installer, application service or renderer is introduced here.

The exec-only launcher gives labwc a private, systemd-owned 0700 local runtime
directory `/run/postcardscene-wayland`, with the first socket `wayland-0`. It uses
packaged configuration rather than the administrator's desktop profile, rejects
stale socket state, and passes an allowlisted environment. Inert bindings prevent
older labwc from loading desktop defaults. Empty composition clears to black
without a wallpaper utility; native Wayland clients receive no X display.

`WaylandSession` validates runtime-directory ownership/mode and returns explicit
child graphics variables. Its bounded core Wayland sync probe reports compositor
availability separately from process activation and physical display state;
optional MainPID matching rejects a different peer. Public diagnostics contain
only fixed safe vocabulary. Systemd owns restart and bounded TERM/KILL cleanup;
RuntimeHost remains application lifecycle authority. Renderers must be runtime
children, never labwc autostart jobs. No generic supervisor is added.

[Operations](operations.md#linux-graphical-session-62) owns the service/PAM/seat
provisioning contract, readiness semantics and evidence limits. #63 implements
connector/EDID/mode/hotplug policy below; #64 owns Chromium isolation/control, #65 shared
surfaces/overlay/input capability, and #66 representative physical validation on
both primary distro paths. Package availability and headless Linux smoke do not
prove Pi/HDMI/4K, non-root seat permissions, acceleration or audio support. #31
remains open until those children and its umbrella acceptance contract are met.

### One-display output policy (#63)

`postcardscene.graphics.output.reconcile_display(session, connector_override=...)`
consumes the existing `WaylandSession` readiness and explicit client environment.
Both primary distro paths use the same Linux DRM/wlr-randr code. Only connected
`card*-HDMI-A-*` sysfs connectors are eligible. Zero is normal `no_display`; one
is selected; multiple are `ambiguous` unless a trusted host override names one
exact `HDMI-A-N`. Invalid/disconnected overrides and duplicate card identities
fail closed. The selected name must match a current Wayland output.

Bounded DRM status/EDID reads supply physical presence and manufacturer/product
codes only. `/usr/bin/wlr-randr --json` supplies advertised modes and current
output state. Description, model text, serials and raw EDID are discarded.
JSON/schema/tool failures become fixed degraded reasons, with no X11 fallback.
The command schema and millihertz selector follow
[upstream wlr-randr](https://gitlab.freedesktop.org/emersion/wlr-randr/-/blob/master/main.c).

Policy selects advertised 3840x2160 at 59.8–60.2 Hz, then 1920x1080 in that
class, then preferred/current modes at <=60.2 Hz; otherwise `no_safe_mode`.
Candidates sort by distance to 60, preferred, current, descending width/height,
then refresh. Numeric selectors use millihertz; indistinguishable timings fail
closed unless a unique preferred mode can be controlled with `--preferred`.
Scale is exactly 1, position 0,0, transform normal. No custom timing, high-refresh,
HDR or VRR selection is introduced. Unselected outputs are not reconfigured.

Reconciliation applies at most once, only if needed, using shell-free argv and
a two-second command deadline with capped 256 KiB stdout and discarded stderr.
Fresh JSON must verify the mode identity, enabled state and layout. A command
exit code alone cannot establish readiness. Immutable `DisplayStatus` snapshots
expose selected connector, EDID codes, current/desired mode, scale and fixed
state/reason; no raw environment, tool output or display serial is public.

`monitor_display(session, stop_event, connector_override=...)` is a blocking
snapshot iterator with an interruptible one-second wait after each reconcile.
The later runtime owner consumes it and supplies its existing shutdown Event;
this change starts no runtime thread/service. Tool waits observe cancellation
at most every 50 ms, with a 250 ms kill/reap allowance. Repeated polls recover
from no-display/connect/disconnect/reconnect and failures without busy retry.
No-display skips the unnecessary Wayland output command and leaves labwc alive.
These are software contracts; #66 owns physical HDMI/4K/hotplug evidence and
#10 owns panel standby/wake. Future power coordination must suspend output
reconciliation while intentionally disabling an output so it is not re-enabled.

### Supervised Chromium controller (#64)

`postcardscene.graphics.chromium.ChromiumController` is the single ordinary-Python
browser capability for both distro paths. Its serialized synchronous API is
`ensure_started`, `navigate`, `blank`, `restart` and `stop`, with bounded operations
and caller cancellation predicates. It consumes `WaylandSession.inspect()` and
`client_environment()` directly; browser/control readiness requires no physical
display. No runtime loop, output monitor or renderer adapter is started here.

`ChromiumLaunchSpec` supplies a trusted absolute package launcher and optional
package invocation words, private profile root and small non-secret environment
overlay. Browser options are code-owned: native Wayland/Ozone, kiosk, suppressed
first-run/restore UI, disabled extensions/plugins, no sandbox weakening or broad
file access, and one fixed black data page. There is no default Chromium path,
distro branch or parent-environment inheritance. #26 provisions actual launchers;
#66 must validate each launcher against this same contract.

`BrowserContext.TRUSTED_IMAGE` accepts only explicit-port `http://127.0.0.1/`
caller URLs with an absolute path and no userinfo/fragment. `UNTRUSTED_WEB` accepts
ordinary HTTP/HTTPS URLs without userinfo; #82 defines the application URL and
session policy above. Caller data/file/script/browser URLs are rejected.
Each root is private, non-root-owned, non-symlinked and context-marked; an existing
personal profile is never adopted. Nested roots and cross-context reuse are
rejected. A profile lock prevents simultaneous owners. The two contexts use
independent process groups, user-data directories and CDP connections. Trusted
state is replaceable, not backup authority; the untrusted-web root follows #82's
persistent-but-replaceable policy above.

V0 uses loopback CDP with OS-selected ephemeral port, discovered only from fresh,
bounded `DevToolsActivePort` metadata under the dedicated profile. Endpoint host
is constructed as `127.0.0.1`, with proxies disabled and no public control API.
The locked `websockets` synchronous client supplies framing, bounded messages and
queues; the private controller limits commands to version/target discovery,
attachment, page lifecycle/navigation and denying downloads. It selects one
initial black page and fails closed on extra/replaced page targets. No generic
CDP, DOM automation or GPU diagnostic API is exposed.

Navigation follows the [CDP Page contract](https://chromedevtools.github.io/devtools-protocol/tot/Page/):
command acceptance alone is insufficient. A matching main-frame/loader load event
(or same-document event) and fresh frame state must agree. Browser error pages,
downloads, dialogs, timeouts and cancellations cannot count as loaded content.
`blank` verifies the fixed black document. Failures retire the browser to expose
labwc's black background; #57 image decode readiness remains a separate assertion
for #58 to compose. Operations expose only `ChromiumError.reason` and a separate
`cleanup_failed` flag, never URLs/tokens, CDP endpoints, stderr or profile data.

Launch uses `start_new_session=True`, never `preexec_fn`. Cleanup retains the
unreaped leader until the final process-group signal, preventing PID reuse even
when a wrapper exits before its children. TERM receives 250 ms, then group KILL
and bounded leader reaping complete cleanup; failed cleanup retains ownership and
prevents replacement. Systemd remains outer supervision. #65 owns shared surfaces,
#66 real package/Wayland/hardware evidence, and #8 later runtime integration.

### Shared surfaces and transient overlay/input (#65)

`graphics.surfaces.ContentSurfaces` is the serialized content-class ownership
seam. It consumes the existing trusted/untrusted `ChromiumController` instances
and `MpvSurfaceProbe` on the same `WaylandSession`; it duplicates neither browser
nor output policy. V0 permits **one active content surface at a time**. Switching
classes stops the previous process group before starting the replacement; labwc's
black background is the intentional interstitial and failure fallback. Failed
cleanup retains ownership and blocks replacement. Same-class use may reuse its
controller. The later runtime owner polls `reconcile` and owns shutdown; this
capability adds no Scene, scheduling, history or automatic restart loop.

`graphics.mpv_probe.MpvSurfaceProbe` creates only an inert fullscreen native
Wayland GPU window, with audio, default bindings, OSC and scripts disabled. Its
trusted absolute host launcher has code-owned flags and bounded process cleanup.
Readiness requires a configured, buffer-backed Wayland surface commit, not merely
a live process. Probe-only Wayland trace is capped, consumed and discarded. No
media loading, JSON IPC, codec/hwdec or playback controls are implemented.

`graphics.overlay.Overlay` launches the system-Python GTK3/gtk-layer-shell
helper; GI remains outside main application imports and uv dependencies. #92
extends #65 with common Previous/Play-Pause/Next and capability-aware video
seek/audio controls. Closed target-free state travels over inherited ASCII JSON
pipes (maximum 1024 bytes including newline); only closed actions return.
The bottom panel uses OVERLAY with zero exclusive zone and genuinely unmaps on
hide. A separate transparent **8 logical-pixel bottom-edge** surface enables
pointer/touch reveal only while controls are hidden and output is not suppressed.
There is no full-screen hidden interceptor or global pointer observation.

`graphics.local_input.InputChannel` carries the same nine typed actions through
an owner-only runtime-directory socket. The versioned `playback-keybindings.xml`
snippet maps fixed compositor keys directly to one absolute emitter plus one
allowlisted action; #26 owns installation. No shell, raw key stream, DOM/CDP
injection, browser control API or additional renderer authority is involved.

`runtime.controls.Controls` owns one cancellable control/input thread suitable
for #93 integration. It forwards actions to #89's bounded mailbox, reads its
snapshot and owns only chrome visibility: hidden initially, valid activity reveals,
five monotonic seconds without activity hides even when paused. Status updates
never reset this timer. Suppression disables both panel and hotspot. Modest bounded
polling services both private event sources; only needed state changes reach the
helper. One pending command outcome provides small local feedback without another
queue. Unavailable controls retire locally and playback can continue; uncertain
cleanup sets shared cancellation and a fixed fatal failure for #93. Join is bounded
to five seconds. This does not construct or modify RuntimeHost/content execution.

[Operations](operations.md#shared-surface-and-overlay-capability-65) records the
protocol, software smoke and limits. #8 owns runtime/control UI integration, #6
real mpv playback, #7 web policy, #10 panel protection, #26 system provisioning,
and #66 representative physical/package evidence on both distro paths.

## 18. Display power management

Stopping playback is not sufficient. During configured sleep periods PostcardScene should attempt to put the physical panel into standby so it is neither a night-time light source nor needlessly active.

Power control remains a separate subsystem from the Linux graphics session.

Preferred methods:

1. HDMI-CEC for compatible TVs.
2. DDC/CI for compatible monitors.
3. DRM/KMS or HDMI-signal control as a fallback where supported.

Settings should allow automatic capability discovery where practical, preferred backend, fallback, wake/handshake delay, test actions, and schedule configuration.

The host/runtime remains alive while the panel sleeps so administration, scheduling, catalog reconciliation, and later provider refresh can continue.

A failed power operation should surface degraded/unknown state rather than falsely claiming the physical panel reached the requested state.

## 19. Burn-in and static-content protection

Panel protection is separate from scheduled sleep.

Potential configurable protections include:

- maximum dwell time for static web/dashboard scenes
- optional subtle position/pixel shifting for appropriate static content
- avoidance of unnecessary permanent overlays
- renderer/player watchdog
- safe blanking if the renderer stalls
- eventual standby after prolonged severe failure

Ordinary changing photography should not be subjected to distracting movement solely for burn-in prevention.

## 20. Web/control application

The web interface should provide purpose-built management pages rather than expose raw database structures.

Expected areas include:

```text
Dashboard
Sources
Scenes
Sequences
Schedules
Display
Playback
Integrations
Users
System
```

The control frontend uses server-rendered Flask/Jinja and locally vendored compiled
Bootstrap 5.3.x (initially 5.3.8). A small PostcardScene CSS-variable layer defines
the visual identity, with dark default and explicit light support through
`data-bs-theme`. Browser-local theme preference is independent of application
settings and projected-scene styling. First-party CSS/JS is served directly as
readable, unminified source; browser code uses small modern vanilla JavaScript
native ES modules. There is no Node, Sass, TypeScript, bundler/minifier, asset
manifest or SPA/HTMX pipeline. Future tooling requires a concrete owning issue;
substantial client state or typed editor/API contracts may justify TypeScript.
See [UI design](ui-design.md) for the visual, accessibility and asset policies,
including image-first projected content with dark translucent information overlays.

The base settings/status UI should be responsive and preserve normal accessible labels, keyboard operation, focus/error feedback, and reasonable contrast.

The Flask development server/debug mode is not the production appliance serving contract.

Issue #13 provides `postcardscene.web.create_app(config=None)`, a `control`
blueprint for the overview, and shared Jinja templates/local CSS. Future
settings, status, source, scene and sequence features should add focused blueprints
under `web` when their owning issues implement them, rather than populate empty
feature modules now. Importing the package or constructing the app starts no
runtime work, background threads or servers.

Configuration precedence is Flask defaults with debug/testing disabled and no
secret, optional operator-owned Python file via `POSTCARDSCENE_CONFIG`, then
explicit mapping overrides. A specified invalid file fails startup. This permits
Flask Host configuration without choosing a production bind or proxy policy;
server binding remains outside the factory and no forwarded-header middleware is
installed. #29/#26 retain production serving ownership. The shell has a lazy shared
database handle and explicit migration CLI; #15 adds authentication, without settings
behavior or runtime IPC. See README for local startup.

Issue #16 adds authenticated Overview/status and Settings. The explicit
`0003_application_settings` migration seeds one typed singleton with timezone `UTC`;
validated IANA names use Python `zoneinfo` and short shared transactions. This is
durable appliance configuration; #9 retains scheduling/DST ownership. Status
composition lives in a small explicit `postcardscene.status` view model using
`Database.check()`, with sanitized failure rows. Later owning issues add concrete
contributors that contain their own failures, without a registry. Runtime is only
unavailable/not yet connected until a later issue selects and wires IPC. Authentication still depends
on a compatible readable database; status does not bypass that boundary.

Issue #25 adds the authenticated filesystem `sources` blueprint: list,
create/edit, POST refresh and POST delete, all mutations CSRF-protected. Forms
validate through `PathPolicy(MEDIA_ALLOWED_ROOTS)` and #22 before short Source
write transactions; normalized configuration goes through the domain seam.
Enabled creation, authority edits and re-enable queue #52 requests after commit;
queue failure preserves the saved Source and offers manual retry. Rename does
not queue; disable and authority invalidation retain #52 semantics. Empty/invalid
roots leave the DB-only list usable but prevent filesystem form saves. Roots are read-only
host guidance; there are no mount controls, credentials or filesystem browser.

A small explicit view model derives health from persisted Source/catalog state,
prioritizing disabled, interrupted, queued, then last result. It shows older
result/freshness, timestamps in the Settings timezone, and #30 grouped counts
without materializing MediaItems or touching storage. Retained counts are not
current availability claims; only a completed authoritative presence scan means
Ready. Database failures produce sanitized 503 feedback. Deletion uses the
existing restrictive domain/FK lifecycle. Schema remains `0008_catalog_requests`.

Issue #82 extends Sources with **Add web Source** and a dedicated structured
name/kind/URL/enabled form, using the shared web semantic validator before saving.
Edit selects the form from the stored Source kind; web/filesystem conversion is
not offered. Login and CSRF protect every page/mutation. URLs are shown only in
the authenticated Sources area. Web saves require no media roots or network probe,
create no Widgets/catalog/refresh requests, and invalid input leaves state intact.
Web cards omit filesystem health/counts/refresh; restrictive deletion is shared.
Filesystem forms, refresh/invalidation and health retain #25's behavior.

Issue #90 adds authenticated `widgets` and `scenes` blueprints with structured
Flask-WTF forms and POST/CSRF mutations. Widget kinds are fixed at creation;
image/pair/video/web forms normalize configuration through #56/#71/#82 validators.
Source compatibility is rechecked inside the short writer transaction, including
disabled compatible Sources. Scene editing replaces one `main` placement through
the domain with layout fixed to `single`; existing splits remain listed as
non-executable in V0, reject edits, and retain restrictive deletion. Disabling
preserves references and related state. These DB-only pages perform no content
probes, catalog requests, rendering, Sequence editing or runtime commands and add
no schema. See the [composition guide](composition.md) for the operator workflow.

Issue #91 adds authenticated Sequence forms with complete domain membership
replacement, repeated occurrences, and server-rendered draft Add/Remove/Move
controls. New membership choices are single Scenes, including disabled Scenes;
existing splits stay visible and must be explicitly replaced or removed before
saving. Settings exposes explicit active Sequence/Idle and fallback dwell through
separate POST/CSRF forms using #87 typed setters. Active Sequence deletion clears
selection through the existing FK. These operations are DB-only, with no schema,
transient playback persistence or runtime IPC; #92/#93 retain controls/integration.

## 21. Authentication, network exposure, and secrets

Initial authentication should be simple and appropriate to a self-hosted appliance.

Expected initial authority:

- administrator
- optional viewer/operator only when a concrete need appears

Issue #15 uses a single SQLAlchemy administrator (explicit `0002_administrator` migration), Werkzeug scrypt hashes, Flask-Login strong session protection and Flask-WTF CSRF. Logout/reset rotate its revocable login identity; all sessions are revoked.

Host CLI commands provision a protected persistent signing file and bootstrap/reset the administrator without database editing. Cookies are HttpOnly/SameSite=Lax, age-bounded to 12 hours; Secure is configurable (false for loopback HTTP development). README owns setup/recovery details; #29 retains production transport and credential-at-rest decisions.

Before V0 release, the project must explicitly define:

- control-interface bind/listen defaults
- HTTP/HTTPS/reverse-proxy support boundary
- Host/proxy assumptions
- session-cookie behavior for the effective transport
- credential-at-rest strategy before long-lived third-party credentials are stored
- master-key/secret recovery behavior

No external telemetry/analytics is enabled by default.

## 22. Privilege boundaries

The normal Flask service must not run with broad root privileges.

Potential privileged/device operations include:

- display/GPU/CEC/DDC device access
- service/install operations
- future controlled mount changes

Prefer normal Linux group/device permissions where possible. If elevation is necessary, use a narrowly scoped helper/service exposing only exact allowlisted operations and validated arguments.

Subprocesses should be invoked with argument vectors rather than untrusted shell construction. Avoid `shell=True` for application-controlled external commands.

## 23. Reliability, logging, and resource bounds

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

## 24. Release, installation, and update boundary

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

## 25. Backup and restore boundary

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

## 26. Documentation and release evidence

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

## 27. Open architectural decisions

The following are intentionally unresolved until the owning issue has enough evidence:

1. **General control-plane/runtime IPC** — Unix socket, localhost HTTP, another narrow local protocol, or a combination. #52 freezes only catalog refresh requests as coalescing SQLite tokens; live status/playback transport remains open.
3. **General cached-provider storage** — exact V1 cache implementation/invalidation strategy.
4. **Credential-at-rest mechanism** — exact protection/master-key approach and recovery behavior.
5. **Wayfarer native integration** — API endpoints/authentication and whether Wayfarer provides a dedicated display-oriented page.
6. **Provider/plugin registration** — whether a formal plugin mechanism ever becomes worthwhile.
7. **Frontend enhancement** — whether HTMX or another small enhancement is justified after the basic Flask/Jinja UI exists.
8. **Scheduling implementation primitive** — exact library/timer implementation behind the frozen scheduling semantics.
9. **Graphics output and renderer integration** — Wayland/labwc session, output/hotplug policy and shared Chromium control and surface/overlay/input capability are frozen by #62–#65; physical validation remains with #66.
10. **Kiosk authenticated-session persistence** — whether V0 persists third-party web-session cookies and how that state is isolated/recovered.
11. **Release artifact format** — wheel/archive/other small managed-native distribution shape, owned by #26.
12. **Production web serving/network boundary** — exact WSGI server and HTTP/HTTPS/reverse-proxy model, owned by #29/#26.
13. **Renderer live-update transport** — ordinary HTTP polling is preferred where adequate; SSE, WebSockets, or another server-push mechanism is selected only if a concrete V1/V2 requirement proves it useful.

These are deliberate implementation decisions, not reasons to invent answers early. When one is resolved, update this document in the same change that relies on the decision.
