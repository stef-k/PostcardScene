# Composition and catalog

Composition persistence, filesystem authority, catalog reconciliation and later provider context/cache boundaries.

Read the [architecture entry point and map](../architecture.md) first. Together,
the overview and linked subsystem documents form the architecture authority.

Rendering semantics are in [Rendering](rendering.md); execution is in [Runtime and scheduling](runtime-and-scheduling.md). The shared database foundation is in [Installation, recovery and operations](installation-recovery-and-operations.md#shared-persistence).

## Core composition model

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

## Filesystem media catalog

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

## Sources and later integrations

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

## Shared context

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

## Data collection and caching

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
