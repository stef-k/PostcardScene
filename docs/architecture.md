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
Python `RuntimeHost`, independent of Flask, database setup and display hardware.
It starts no renderers, scans, schedules or workers. `host.status` returns a frozen
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
not the future concurrency primitive; no job framework or third service is added.
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

The complete V0 layout vocabulary and canonical region positions are:

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

A filesystem-backed `MediaItem` may hold normalized data such as:

- stable source-relative/canonical identity
- media type
- locator/path identity
- image dimensions/orientation
- video duration where proportionately obtainable
- freshness/modification identity
- capture timestamp where safely and usefully obtainable
- availability/stale/error state

Original image/video bytes remain in the source filesystem.

### Reconciliation rules

Cataloging must distinguish:

- newly discovered item
- changed item requiring metadata refresh
- confirmed removed item
- source temporarily unavailable
- item temporarily unreadable

A NAS outage must never be interpreted as authoritative deletion of the entire catalog.

Large scans must be bounded/cancellable and use short database transactions. Reconciliation is owned by the long-running runtime/background-work boundary, not synchronous Flask requests.

V0 does not require thumbnails, computer vision, face recognition, broad EXIF indexing, or a generic catalog for future provider assets.

Whether catalog state is backed up as useful durable state or treated as a regenerable optimization must be decided by #30/#27 and recorded in the backup contract.

## 7. Image behavior

PostcardScene V0 should support:

- full-screen landscape images
- portrait images
- intelligent consecutive portrait pairing
- correct orientation using presentation/EXIF semantics
- deterministic fit/fill/crop behavior
- configured dwell times
- transitions
- ordered/shuffled selection through stable media identities
- useful quality for 1080p/validated-4K display without unnecessary source recompression
- intentional color/profile behavior with documented platform limitations
- bounded decode/load behavior for malformed, extremely large, or unreachable media

Portrait pairing is a core feature. When eligible consecutive media are portrait-oriented and compatible with the current layout rules, the runtime can display them side-by-side to use a landscape screen efficiently.

PostcardScene should not rewrite original photographs merely for ordinary playback.

## 8. Video and audio behavior

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

The runtime executes the composition model and owns transient playback state.

Useful transient state may include:

- current scene/membership
- recent-play history
- previous/next navigation state
- current selected MediaItems
- renderer health

This state should not be pushed into configuration models unless a concrete restart requirement justifies a small durable field.

When content is unavailable, the runtime must use bounded skipping/fallback behavior. It must not enter a tight retry loop.

If no eligible content remains, the display should converge to a defined safe blank/idle state and may invoke panel-protection behavior rather than leave stale content indefinitely.

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

The exact boundary between Chromium-rendered images and any dedicated/native image-rendering path remains open.

The runtime/player service coordinates the active scene and Chromium/mpv processes.

## 11. Web-content isolation

Configured web pages are untrusted renderer content even when the administrator chose the URL.

V0 web scenes should normally accept only supported HTTP/HTTPS URLs. Dangerous local/script/browser-extension schemes such as `file:` or `javascript:` are not ordinary web-scene inputs.

The kiosk browser must use a dedicated profile/runtime state separate from the administrator's normal browser and from PostcardScene's control-plane session cookies/secrets.

Persistent third-party kiosk login/session support, if implemented in V0, must use an explicit isolated mechanism. Generic credentials/tokens should not be embedded in URLs merely to display private pages.

Public/share-token display pages such as a purpose-built Wayfarer display/share view are the simpler preferred integration before native structured Wayfarer support exists.

Production Chromium must not expose unnecessary remote-debug/automation listeners. If a control mechanism requires one, it must be bound to a narrow local authority.

Navigation/load retries and timeouts must be bounded so one unreachable site cannot monopolize a sequence indefinitely.

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

PostcardScene is a display appliance and must explicitly own its graphical session.

Issue #31 must select the smallest supported production graphics/session model for the target Linux baseline. Candidate approaches may include a minimal Wayland compositor, X11 session, or direct KMS-capable arrangement.

The selected path should avoid a general desktop environment or interactive display manager unless evidence proves one is necessary.

The graphical runtime must define:

- which user owns the session
- how it starts under boot/systemd ownership
- how Chromium and mpv target the same display
- safe blank/background behavior before renderers are ready
- EDID/display connector discovery
- deterministic 1080p/4K mode/refresh/scaling behavior
- boot with no display attached
- HDMI disconnect/reconnect
- GPU/render/video device permissions
- hardware acceleration/video decode expectations
- audio-output ownership relevant to video playback

Hardware-specific support claims require physical representative ARM64/Pi evidence. Generic CI cannot prove HDMI/GPU behavior.

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
- media catalog as durable or regenerable
- manifest with application/schema/archive identity
- checksums

It excludes external media libraries and replaceable caches by default.

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

1. **Control-plane/runtime IPC** — Unix socket, localhost HTTP, another narrow local protocol, or a combination.
2. **Image rendering boundary** — all images in Chromium vs a dedicated rendering path for some modes.
3. **General cached-provider storage** — exact V1 cache implementation/invalidation strategy.
4. **Credential-at-rest mechanism** — exact protection/master-key approach and recovery behavior.
5. **Wayfarer native integration** — API endpoints/authentication and whether Wayfarer provides a dedicated display-oriented page.
6. **Provider/plugin registration** — whether a formal plugin mechanism ever becomes worthwhile.
7. **Frontend enhancement** — whether HTMX or another small enhancement is justified after the basic Flask/Jinja UI exists.
8. **Scheduling implementation primitive** — exact library/timer implementation behind the frozen scheduling semantics.
9. **Linux graphics/session model** — exact Wayland/X11/KMS/compositor path, owned by #31.
10. **Kiosk authenticated-session persistence** — whether V0 persists third-party web-session cookies and how that state is isolated/recovered.
11. **Media catalog backup classification** — useful durable state vs regenerable optimization, owned by #30/#27.
12. **Release artifact format** — wheel/archive/other small managed-native distribution shape, owned by #26.
13. **Production web serving/network boundary** — exact WSGI server and HTTP/HTTPS/reverse-proxy model, owned by #29/#26.
14. **Renderer live-update transport** — ordinary HTTP polling is preferred where adequate; SSE, WebSockets, or another server-push mechanism is selected only if a concrete V1/V2 requirement proves it useful.

These are deliberate implementation decisions, not reasons to invent answers early. When one is resolved, update this document in the same change that relies on the decision.
