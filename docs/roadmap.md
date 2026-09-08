# Roadmap

This roadmap keeps the first implementation deliberately smaller than the complete PostcardScene vision while preserving the architecture needed for richer scenes later.

The phases describe capability boundaries rather than fixed release dates.

## Foundation / V0

Goal: prove that PostcardScene can operate reliably, securely, and recoverably as an unattended media-display appliance before expanding into rich external-data integrations.

### Application foundation

- Python project/package structure
- repeatable dependency/test/lint development workflow
- Flask web/control application
- SQLAlchemy + SQLite persistence
- explicit database migrations/schema identity
- local authentication and host-authorized administrator recovery
- responsive settings/status UI foundation
- separate web/control and long-running runtime/player services
- long-running/background work kept outside Flask requests
- status/health information sufficient to diagnose degraded subsystems

### Core domain

Implement the foundational composition model from the beginning:

```text
Source -> Widget -> Scene -> Sequence
```

V0 only needs a small number of concrete implementations, but media playback should not bypass this model with a separate throwaway slideshow architecture.

Filesystem `MediaItem` catalog records are normalized data produced beneath a `Source`; they do not become a fifth universal composition concept.

### Media sources and catalog

- local directory source
- mounted network-directory source
- SMB/NFS documented as normal Linux mounts
- safe canonical path/symlink boundaries
- source availability/error handling with bounded network-filesystem behavior
- persistent filesystem media catalog suitable for large libraries
- normalized V0 media identity/metadata
- incremental add/change/remove reconciliation
- source outage distinguished from authoritative item deletion
- bounded/cancellable catalog scans owned by the runtime/background-work boundary
- authenticated filesystem Source management, persisted catalog health/counts,
  and runtime-owned manual refresh are operational through the Sources UI (#25)

Do not copy the user's original media into PostcardScene merely to index it.

### Image playback

- image-specific catalog eligibility and immutable Source-relative selection/freshness (#56)
- automatic pairing of two consecutive distinct portraits, with landscape/square/unpaired single fallback (#56)
- caller-owned first/lookahead grouping with consumed count; no image-owned stream
- `fit=contain|cover`, default contain, with fixed black background and centered position
- correct orientation/EXIF presentation behavior
- Chromium image presentation through trusted disposable delivery (#57) and the shared #31 controller (#58)
- useful 1080p/validated-4K quality without unnecessary source recompression
- intentional color/profile behavior and actual browser-format evidence (#59)
- bounded loading/decode behavior for malformed, huge, unreachable or unsupported media
- optional simple fixed image-local fade in #57, without Widget transition configuration

#5 owns image eligibility/grouping/presentation/quality. Catalog metadata readiness
does not establish Chromium format support. Scene/Sequence duration configuration
belongs to #19/#20; runtime dwell, order/shuffle, history and global transport
(Previous/Next/Play/Pause/common controls) belong to #8. Panel safety remains #10.

### Video and audio playback

- mpv integration
- local/network video files through the common catalog
- play-full-duration behavior
- controlled skip/failure/load-timeout behavior
- recovery from player/media failure
- hardware decode where supported by the validated runtime
- default-silent Widget audio (`audio_enabled=false`, integer `volume=50`, bounded 0–100); mute stays transient
- intended HDMI/audio-output behavior where supported
- no orphan audio after scene change, skip, sleep, or player restart
- honest supported codec/container boundary rather than universal-format claims

#71 implements DB-backed video configuration/selection: immutable Source-relative
identity and size/mtime snapshots, bounded catalog lookup/pages, no filesystem
scan/open or decoder-support promise. Image metadata and catalog `duration_ms` are
not prerequisites; active duration comes from the player. Intended audio-device
selection remains a trusted runtime input. #8 owns order/shuffle/history and global
transport UI. #73 implements supervised playback and #74 adds live state,
idempotent pause/resume and bounded seek beneath #8 controls. Physical support
claims remain gated by #66/#76. #72 adds fresh pinned read-only FD authority,
including bounded spawned mounted safe-open and private opened-FD transfer.
#73 consumes pinned descriptors in a fresh mpv child; #8 still owns runtime integration.
#75 adds opt-in audio, capped initial/transient volume, mute, trusted automatic/explicit
device policy and fail-silent retirement. No-track/device failures preserve silent
video where mpv can continue. #10 sleep integration and #66/#76 physical HDMI/audio
evidence remain open.

### Basic web scenes

#82 implements HTTP/HTTPS URL semantics, immutable web-target resolution and
authenticated structured web Source management. The isolated untrusted-web profile
may persist ordinary site state but remains replaceable/non-backup state. Rendering
is supplied by #83 with bounded fresh navigation, one fresh-browser retry and
black fallback; physical web support #84 remains gated on #31.

- supported HTTP/HTTPS URL scene/widget
- Chromium kiosk integration
- isolated kiosk browser profile/session separate from PostcardScene administration
- safe URL scheme validation
- configurable duration and bounded load/retry behavior
- basic renderer supervision/restart and safe blanking
- intentional support for local-network URLs within the documented security boundary
- any private/authenticated third-party page uses an explicit isolated session mechanism rather than generic credentials embedded in URLs

This is sufficient to display a Wayfarer share/display URL before native Wayfarer integration exists.

### Basic scenes and sequences

- single-content scene
- portrait-pair scene
- `single`-Scene execution only; persisted split layouts reserved for V1 composition
- scene duration
- ordered/shuffled sequences
- small recent-play/runtime history where useful
- manual next/previous/pause/resume controls where appropriate
- bounded skipping/fallback when sources/items fail
- safe blank/idle behavior when no eligible content remains

#87 supplies explicit nullable active-Sequence selection (`None` means safe idle),
immutable configuration/occurrence snapshots, and default image/web dwell of 30
seconds (integer 1–86400). Membership duration overrides Scene duration; otherwise
image/portrait-pair/web use default dwell and video uses natural EOF. Explicitly
timed video ends at EOF or its deadline, whichever occurs first. Panel-safety
thresholds remain separate. Runtime restart discards cursor/history/shuffle state;
configuration is reread at display-step boundaries.

#88 supplies pure bounded display-step planning: canonical ordered media, shuffled
membership cycles, per-Widget media streams with 32 consumed recent identities,
caller-owned portrait lookahead, and 32 revalidated Previous/Next history steps.
It adds no runtime worker, timer, rendering or controls; #89 owns execution.

The portrait pair is one Widget in a `single` Scene. Existing split layouts remain
valid persistence but are explicitly non-executable in V0. Generic multi-region
execution belongs to V1 rich composition. Playback, controls and composition UI
remain later #8 children; #93/#94 retain their image/physical prerequisite gates.

### Operating schedule

#101 implements durable weekly active windows and pure IANA-timezone evaluation:

- at most 64 same-day half-open active windows, Monday 0 through Sunday 6;
- OR semantics, overlaps allowed, full day 0–1440 minutes;
- overnight spans require two explicit adjacent-day rows;
- disabled means active; enabled with no windows means sleep;
- current UTC instant converted through the application timezone: skipped DST
  minutes never occur, repeated minutes obey the same rule both times;
- one durable active/sleep override for integer 1–10080 minutes, absolute UTC
  expiry and compare-clear cleanup of observed expiration;
- atomic configuration replacement and detached immutable snapshots.

#102 adds one independently constructible runtime schedule thread: immediate
current-state evaluation, a fixed 15-second polling budget, cooperative shutdown,
and one bounded injected operating target. Ordinary failures degrade/retry; target
cleanup uncertainty stops the runtime. No transition replay or queue is added.
#103 adds authenticated server-rendered weekly configuration and bounded
temporary overrides, with a request-time configured decision and DB-only saves.
Live playback/panel RuntimeHost wiring (#104) remains later work; physical panel
behavior is not claimed.

### Display power and panel protection

#110 persists configuration only in the singleton appliance settings: closed
`auto|cec|ddc|signal` backend choice (default `auto`, capability priority CEC ->
DDC -> signal; explicit selection is strict), integer wake handshake delay 0–30
seconds (default 5), and non-disableable static safety ceiling 300–14400 seconds
(default 1800). Invalid replacements preserve prior policy; damaged storage fails
explicitly. Safety age is independent of playback dwell and continues during Pause;
new presentations, including successful Previous/Next, reset it, while overlay
reveal and schedule Keep active do not bypass it. Images/web are potentially static;
progressing unpaused video is not static solely due to unchanged Scene identity.
#111 adds bounded CEC/DDC/signal capabilities with verified physical or signal-only
observations and explicit cleanup uncertainty. #112 adds the independently
constructible single panel coordinator, closed protection/diagnostic/operating
precedence, bounded convergence and wake readiness. RuntimeHost wiring, live
protection/playback enforcement, Display UI and physical evidence remain later
#10 work, as defined in the
[Graphics and panel contract](architecture/graphics-and-panel.md).


- display-power abstraction
- configurable backend selection
- HDMI-CEC backend
- DDC/CI backend
- DRM/KMS or HDMI-signal fallback where supported
- configurable wake/handshake delay
- test on/off controls from authenticated settings
- safe/degraded behavior when a preferred backend is unavailable
- maximum static-content dwell safeguards where relevant
- renderer/player watchdog
- safe blanking when playback/runtime stalls
- fallback toward panel standby for prolonged severe failure
- display sleep always silences/stops media audio

### Linux graphics/runtime and unattended operation

- separate `postcardscene-web` and runtime/player systemd services
- service ordering and restart behavior
- standard runtime user/group and durable-state/cache/log/device ownership expectations
- shared native Wayland/labwc session on Raspberry Pi 4/5 ARM64 with Ubuntu Server LTS and Raspberry Pi OS 64-bit as primary V0 targets (#62)
- dedicated non-root user, distro-appropriate logind/libseat or seatd authority, no full desktop/display manager; provisioning remains #26
- Chromium and mpv target the same intended HDMI display/session
- safe blank/background state during boot/renderer restart
- EDID/display discovery and deterministic mode selection
- 1080p minimum and physically validated supported 4K baseline
- boot with no display connected plus later hotplug/reconnect
- hardware acceleration/video decode/audio-output baseline
- bounded cache/log growth and disk-space diagnostics
- host reboot/power-loss and runtime failure recovery
- representative Raspberry Pi-class ARM64 physical evidence for hardware-specific support claims

This work owns the behavior of an already-installed runtime. Physical panel standby is a separate subsystem, and release packaging/install/update is owned separately.

### Release engineering and managed installation

- GitHub Actions CI for supported Python/runtime combinations
- deterministic dependency resolution/locking
- coherent formatter/linter/static-analysis policy
- dependency vulnerability inventory for release candidates
- supported OS/Python/architecture matrix
- authoritative version identity
- versioned GitHub Release artifacts
- final-artifact checksums
- clean-environment installation/start smoke testing
- standard native-Linux installation layout separating payload, configuration/secrets, durable state, cache/runtime state, and backups
- managed installation/bootstrap path using an isolated project-owned Python environment or equivalent supported mechanism
- dedicated runtime user/device-group/directory creation
- dependency checks for Python, graphics stack, Chromium, mpv, and display tools
- first-run administrator setup
- explicit database migration on install/update
- previous-release migration/upgrade testing once more than one release exists
- safe forward update preserving durable state with a verified pre-update backup boundary
- truthful partial-failure behavior
- safe removal/reinstall preserving durable state/backups by default
- stable status/doctor-style diagnostics
- exact release-candidate evidence tied to source/tag/version/artifact checksum
- release and upgrade notes

The exact release artifact format is intentionally open until implementation compares the smallest practical Python-native options. V0 does not require a `.deb`, APT repository, Docker image, auto-update daemon, or transactional rollback engine.

### Backup and restore

Back up PostcardScene's own durable state, not the user's external media libraries.

V0 should provide:

- authoritative durable-state inventory
- SQLite-consistent backup rather than blind live-file copy
- database/archive integrity verification
- application/schema/archive version identity
- required installation-owned secret/key state where credentials must remain recoverable, or explicit credential-reentry classification
- filesystem media catalog classified as regenerable derived state; require fresh reconciliation after restore
- local or administrator-provided already-mounted backup destination
- manual and scheduled backup
- integrity checksums
- atomic publication of complete backups
- bounded retention of owned archives only
- backup listing/freshness/verification
- deliberate in-place restore
- deliberate clean replacement-host/cross-host restore
- disposable restore drill
- post-restore database/schema/secret-state validation
- pre-update recovery integration where schema/state changes justify it

External photos/videos, NAS libraries, Immich assets, replaceable caches, thumbnails, and logs are excluded by default.

### Security, trust boundaries, and secrets

- explicit control-interface bind/network/HTTP/HTTPS/reverse-proxy support boundary
- persistent protected session/application secret
- secure password/session/CSRF behavior
- bounded login abuse protection
- credential-at-rest strategy before long-lived third-party credentials are introduced
- deliberate local administrator/password recovery
- isolated untrusted Chromium web-scene profile
- safe URL schemes and no arbitrary local-file/script execution through web scenes
- validated filesystem and subprocess arguments
- unprivileged Flask process; narrow privileged helpers only where required
- redaction of secrets from errors/logs/status/doctor/backups
- no external telemetry/analytics by default
- release dependency/advisory and artifact-secret checks
- bounded end-to-end V0 trust-boundary closure review

V1/V2 provider integrations must extend this security/privacy model for outbound HTTP, credentials, retention, and third-party data handling.

### Documentation and operator guidance

Documentation is part of feature completion rather than a final cleanup stage.

V0 documentation should remain lean while covering:

- development/test/dependency workflow once stable
- supported/tested Linux/Python/architecture/hardware expectations
- installation, first-run, upgrade, safe removal/reinstall
- service/status/log/disk troubleshooting
- media source/catalog/reconciliation behavior
- image/video/audio behavior and limitations
- graphics-session and tested 1080p/4K behavior
- display-power configuration and practical CEC/DDC/DRM limitations
- timezone/DST scheduling behavior
- control-network/security/secrets/password-recovery boundary
- backup, verification, in-place restore, and replacement-host recovery
- MIT project license plus required third-party notices/attributions before public distribution
- release notes/changelog discipline
- responsive/accessibility baseline for the control UI

Prefer updating the existing authority documents and, when needed, one cohesive `docs/operations.md` rather than creating many small documents prematurely.

### V0 completion criteria

V0 is successful when a supported Raspberry Pi-class Linux system can consume a verified PostcardScene release through the documented native installation path, boot into a controlled safe graphics/display state, start PostcardScene unattended, maintain a bounded local/NAS media catalog, play correctly presented images and controlled video/audio, display isolated supported URLs, compose basic scenes, obey timezone-correct schedules, sleep/wake the physical display reliably, degrade safely through source/media/renderer/display failures, expose a documented secure administration boundary, create and verify a complete application-state backup, restore it in place and onto a clean replacement host, and provide sufficient operator documentation to install, diagnose, update, remove/reinstall, back up, recover, and secure the appliance without reading implementation code.

Before closing V0, the exact candidate must also pass its release-readiness, security, backup/restore, physical-hardware, documentation/license/notice, and no-known-blocker closure gates tracked by the V0 issue.

Do not postpone reliability, security, recovery, or operability work merely to add additional content providers.

---

## Rich scenes / V1

Goal: move from a capable media appliance to the richer ambient-information experience envisioned for PostcardScene.

### Immich

- Immich source/provider
- authenticated connection configuration
- image/video asset metadata
- orientation/dimensions
- capture time
- geolocation where present
- source/album selection as supported by the chosen integration
- use Immich assets in normal image and photo-strip widgets

### Weather

- provider abstraction
- explicit-location weather source
- configurable refresh/cache behavior
- weather widget(s)
- graceful stale-data behavior

### News and feeds

- RSS/Atom source using `feedparser`
- configurable feeds
- age/headline limits
- refresh interval
- headline/news widget
- optional media/image handling where feed data supports it safely

### HTTP/data sources

- shared `requests`-based HTTP client abstraction
- connection reuse where useful
- explicit timeouts
- bounded retries
- generic HTTP/JSON source foundation
- outbound URL/SSRF/privacy rules derived from the V0 security boundary

### Rich composition

Expand the scene system to support combinations such as:

```text
Photo + Weather
News + Weather
Photo + Text/Context
Map/Web + Information Panel
Photo Strip + Main Content
```

Include:

- multiple widgets per scene
- layered/region layouts
- reusable layout definitions or templates where useful
- per-widget configuration
- richer transitions
- scene preview/editing workflow in the web UI

### Data refresh and cache

- provider refresh jobs owned by the long-running runtime/background-work boundary unless a concrete reason changes it
- local cached state
- source freshness/error metadata
- rendering from cached state rather than blocking directly on normal remote requests

### Live renderer updates

Use ordinary authenticated HTTP polling where it provides the required update cadence. This keeps the Flask control plane synchronous and avoids adding persistent-connection plumbing before it is needed.

If a concrete live-scene requirement demonstrates a material benefit from server push, evaluate the smallest suitable mechanism at that time, such as SSE or WebSockets. Do not preselect Flask-Sock, Socket.IO, an ASGI migration, a message broker, or other event infrastructure merely to anticipate future live updates.

### V1 completion criteria

V1 is successful when PostcardScene can create polished mixed-content scenes from local/Immich photography plus current information such as weather and news, while continuing to operate gracefully and securely when remote sources are slow or unavailable.

---

## Contextual travel / V2

Goal: exploit the relationship between PostcardScene, Wayfarer, location-aware media, and live travel to create the project's most distinctive experience.

### Native Wayfarer integration

In addition to generic URL display, add structured Wayfarer access for data such as:

- live/current location
- shared timeline state
- current trip
- route/timeline information
- relevant place information

Coordinate with Wayfarer design as needed to define a clean native API and/or dedicated display-oriented timeline/map view.

### Shared location context

Allow live/current location to become reusable context for independent widgets/providers.

Example:

```text
Wayfarer live location
        |
        +--> map/live marker
        +--> local weather
        +--> local time/place context
        +--> nearby Immich photographs
```

The components should remain separately reusable rather than becoming one hard-coded "Wayfarer scene" implementation.

### Location-aware Immich selection

- query/select assets near the current or selected location
- configurable geographic radius
- recent-upload filtering
- historical-nearby-photo options
- photo strip or scene recommendations based on location

### Live postcard scenes

Support polished compositions such as:

```text
Wayfarer live map
+ current place
+ local weather
+ current local time
+ recent/nearby Immich photos
```

This is the clearest expression of the PostcardScene name: a dynamic postcard combining a place, imagery, and live context.

### Conditional scenes

Allow scenes to become eligible based on meaningful conditions, for example:

- live Wayfarer location is available
- recent nearby Immich uploads exist
- source data is sufficiently fresh
- a configured weather condition is present
- a trip/live-sharing state is active

Conditions should complement time-based sequences rather than replace them.

### Richer presentation

Potential V2 improvements include:

- additional layout templates
- more sophisticated but restrained animations
- crossfades/slides/zoom effects
- optional Ken Burns-style image movement
- richer map/context overlays
- improved scene transition choreography

Visual richness must not compromise reliability, privacy, security, or panel-safety behavior.

### V2 completion criteria

V2 is successful when travel activity can drive the display context automatically: Wayfarer identifies where the live journey is, weather and place information follow that location, Immich supplies relevant imagery, and PostcardScene composes the result into a coherent live ambient scene.

---

## Beyond V2

Possible future capabilities should be driven by actual use rather than committed prematurely. Candidates may include:

- additional media/photo services
- additional weather/news/data providers
- Home Assistant or home-dashboard integrations
- multiple physical displays
- reusable/shared scene packs
- more formal provider/plugin registration
- richer rule/condition systems
- Debian package/APT distribution if native release installation demonstrates a real need for it
- configuration-only export/import if full backup/restore proves insufficient for portability
- optional mobile-native remote control only if the responsive web UI is not enough

These are not foundation requirements unless explicitly promoted into an earlier milestone.

## Roadmap rule

When choosing between adding another provider and making the existing display runtime more reliable, secure, recoverable, installable, or operable, prefer the latter until the Foundation completion criteria are satisfied.

The project should grow outward from a stable display appliance, not inward from a large collection of integrations.
