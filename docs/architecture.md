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
Alembic / Flask-Migrate
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
The initial package version is `0.0.0` and runtime dependencies are empty.

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

Because the web process and runtime/background work may access the same database, V0 must explicitly choose suitable SQLite journaling, timeout, connection, and transaction conventions. Long write transactions should be avoided. Database schema changes are owned by migrations rather than silent application-startup mutation.

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

## 5. Core composition model

The foundational composition model is:

```text
Source -> Widget -> Scene -> Sequence
```

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

A scene defines layout, widgets, visual presentation, duration/default dwell behavior, and only the V0 safety/presentation hints that are concretely required.

Ordinary source-driven scenes should not permanently store whichever media item happened to be selected at runtime. Explicitly pinned-media scenes may do so when that is the intended user configuration.

### Sequence

A `Sequence` determines how scenes are presented.

A sequence may specify:

- ordered or shuffled scene membership
- scene duration/defaults
- full-duration video behavior
- transition style
- weighting/frequency where required
- fallback behavior

Operating-hour scheduling remains a separate subsystem. Future conditional-scene eligibility is not part of the foundational V0 sequence model unless a concrete V0 requirement promotes it.

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

The frontend should remain simple initially. Server-rendered Flask/Jinja pages with modest JavaScript are sufficient until a concrete interaction justifies more. HTMX remains an option, not a requirement.

The base settings/status UI should be responsive and preserve normal accessible labels, keyboard operation, focus/error feedback, and reasonable contrast.

The Flask development server/debug mode is not the production appliance serving contract.

## 21. Authentication, network exposure, and secrets

Initial authentication should be simple and appropriate to a self-hosted appliance.

Expected initial authority:

- administrator
- optional viewer/operator only when a concrete need appears

Use secure password hashing, session protection, CSRF protection, and a persistent installation-owned application/session secret.

A host-authorized local administrator password-reset path must exist without requiring manual database editing or an unauthenticated email-reset service.

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
6. **Scene-layout persistence** — normalized database models, JSON scene definitions, or a hybrid.
7. **Provider/plugin registration** — whether a formal plugin mechanism ever becomes worthwhile.
8. **Frontend enhancement** — whether HTMX or another small enhancement is justified after the basic Flask/Jinja UI exists.
9. **Scheduling implementation primitive** — exact library/timer implementation behind the frozen scheduling semantics.
10. **Linux graphics/session model** — exact Wayland/X11/KMS/compositor path, owned by #31.
11. **Kiosk authenticated-session persistence** — whether V0 persists third-party web-session cookies and how that state is isolated/recovered.
12. **Media catalog backup classification** — useful durable state vs regenerable optimization, owned by #30/#27.
13. **Release artifact format** — wheel/archive/other small managed-native distribution shape, owned by #26.
14. **Production web serving/network boundary** — exact WSGI server and HTTP/HTTPS/reverse-proxy model, owned by #29/#26.
15. **Renderer live-update transport** — ordinary HTTP polling is preferred where adequate; SSE, WebSockets, or another server-push mechanism is selected only if a concrete V1/V2 requirement proves it useful.

These are deliberate implementation decisions, not reasons to invent answers early. When one is resolved, update this document in the same change that relies on the decision.
