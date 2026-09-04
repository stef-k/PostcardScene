# Architecture

This document captures the initial architecture for PostcardScene. It is intended to preserve the decisions already made while leaving implementation details open where there is not yet enough evidence to freeze them.

## 1. Product shape

PostcardScene is best understood as a small self-hosted display appliance rather than a slideshow application.

Photography is the primary use case and travel is an important part of the project's identity, but the runtime is intentionally broader. It should be able to combine:

- images
- portrait image pairs
- video
- web pages
- live maps
- weather
- news/RSS
- location/context data
- generic remote data
- future information widgets

The target is a Raspberry Pi-class Linux host connected to a modern television or monitor. 1080p is the minimum intended display resolution; 4K is preferred.

The application should remain portable enough to run on other Linux systems rather than hard-coding itself to Raspberry Pi hardware.

## 2. Design principles

### The display experience is primary

The web application exists to configure and control the display. It is not the core of the product.

### Rich scenes without a heavy server architecture

Complexity should live in content selection, composition, rendering, context, scheduling, and hardware integration rather than in unnecessary web-framework or distributed-system machinery.

### Graceful degradation

A failed weather API, disconnected NAS, malformed video, or crashed browser must not take down the entire appliance.

### Configuration through the product

Important operating behavior should be tunable from the PostcardScene web interface rather than requiring users to edit service files or scripts for normal use.

### Do not overbuild the first release

The architecture must allow rich contextual scenes later, but the first implementation should prove reliable media playback, scheduling, display control, and composition before expanding into every possible integration.

## 3. Technology direction

Initial application stack:

```text
Python
Flask
SQLAlchemy
SQLite
Alembic / Flask-Migrate
Flask-Login
Flask-WTF
requests
feedparser
Flask-Sock (likely, if/when WebSockets are required)
Pillow
```

Runtime/system components:

```text
Chromium
mpv
systemd
SMB / NFS
HDMI-CEC tools
DDC/CI tools
Linux DRM/KMS display control
```

### Why Flask

PostcardScene needs a proper web settings/control surface, authentication, forms, persistence, and APIs, but the web layer is not the main application runtime.

Flask plus a small set of mature libraries provides the required web capabilities without making the rest of the application conform to a larger full-stack framework.

The architecture should not turn into a large collection of Flask extensions. Ordinary Python should be used where framework integration is unnecessary.

### Why SQLite initially

Configuration, schedules, scene definitions, source metadata, cache metadata, and user accounts do not initially require a separate database server.

SQLite keeps installation and recovery simple. Large media objects stay in filesystems or external services rather than in the database.

## 4. High-level architecture

```text
                         PostcardScene
                              |
              +---------------+---------------+
              |                               |
        CONTROL PLANE                   DISPLAY PLANE
              |                               |
           Flask                         Player service
              |                               |
      Settings / Auth                    Scene engine
      Sources / Scenes                   Sequence engine
      Schedules / API                    Scheduler
      SQLAlchemy                         Context/state
              |                               |
              +--------- shared state --------+
                                              |
                         +--------------------+-------------------+
                         |                    |                   |
                     Chromium               mpv          Display power
                   scene renderer       video playback     CEC/DDC/DRM
```

The control plane and display plane should be separate long-running processes/services.

A Chromium, mpv, or media failure must be recoverable without losing access to the web administration interface. Conversely, restarting the web service should not unnecessarily destroy display state.

The precise inter-process communication mechanism is intentionally not frozen yet.

## 5. Core domain model

The foundation is:

```text
Source -> Widget -> Scene -> Sequence
```

### Source

A `Source` provides content or data.

Examples:

- local directory
- mounted SMB/NFS directory
- Immich
- Wayfarer
- weather provider
- RSS/Atom feed
- generic HTTP/JSON endpoint
- arbitrary URL/web source

A source should expose normalized data to the rest of the system where possible instead of forcing scene code to know provider-specific details.

### Widget

A `Widget` renders one kind of information or media.

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

Widgets consume source data and/or shared scene context.

### Scene

A `Scene` is the primary visual composition shown on the display.

A scene may be simple:

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

A scene should define layout, widgets, visual layers, transitions, duration/default dwell behavior, and conditions where relevant.

### Sequence

A `Sequence` determines how scenes are presented over time.

A sequence may specify:

- ordered or shuffled scenes
- scene duration
- full-duration video behavior
- transition style
- weighting/frequency
- time/day applicability
- conditions
- fallback behavior

Example:

```text
Morning sequence

Photo                     45 sec
Photo                     45 sec
News + Weather             2 min
Portrait Pair             45 sec
Wayfarer Live              3 min
Photo sequence             5 min
Video                      full duration
```

## 6. Scene composition and rendering

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

The exact boundary between Chromium-rendered images and any dedicated/native image rendering remains open. Chromium is the default direction because it allows the same scene system to handle both pure photography and complex mixed-information layouts.

Video playback should use mpv rather than browser video where practical, especially where codec support, hardware acceleration, reliability, or playback control benefits from mpv.

The player service is responsible for coordinating the active scene and the underlying Chromium/mpv processes.

## 7. Media behavior

### Images

PostcardScene should support:

- full-screen landscape images
- full-screen portrait images where requested
- intelligent side-by-side portrait pairing
- configurable fit/fill behavior
- configured dwell times
- transitions
- shuffle/order rules
- image metadata where useful

Portrait pairing is a core feature. When consecutive images are portrait-oriented and compatible with the current scene/layout rules, the system should be able to display them side-by-side to use a landscape screen efficiently.

### Video

Video should support:

- local/network files
- appropriate hardware-accelerated playback where available
- play-full-video behavior
- optional duration/skip policies
- recovery from malformed or unavailable media

## 8. Sources and integrations

### Local storage

Local directories are first-class media sources.

### Network storage

SMB/NFS should normally be mounted by Linux and exposed to PostcardScene as filesystem paths.

The application should not implement its own SMB/NFS filesystem client unless a concrete need emerges.

If mount management is later exposed through the web UI, privileged work must happen through a narrow controlled mechanism rather than by giving Flask arbitrary root access.

### Immich

Immich should be an adapter/source rather than a special case embedded throughout the player.

Useful data may include:

- asset IDs
- URLs/paths
- media type
- dimensions/orientation
- capture time
- geolocation
- album/source grouping

This data can then drive normal image widgets, photo strips, location-aware selection, and future scene types.

### Wayfarer

PostcardScene should support two Wayfarer modes:

1. ordinary URL display in Chromium
2. a native structured-data integration

The native integration is expected to make Wayfarer especially valuable as a context source.

Potential Wayfarer data:

- current/live position
- live timeline state
- current trip
- route/timeline data
- place information
- shared-location state

A dedicated Wayfarer display/timeline mode may later provide a cleaner kiosk-oriented map than embedding the full normal web UI.

### Weather

Weather should be provider-based and capable of using explicit locations or shared current-location context.

### News

Prefer RSS/Atom feeds where suitable, using `feedparser`, rather than scraping arbitrary news web pages.

Provider APIs can be added where they offer a clear advantage.

### Generic HTTP/JSON

A generic source type should eventually allow selected remote JSON/text data to feed widgets without requiring a bespoke integration for every useful service.

Use a shared HTTP client abstraction around `requests`, persistent sessions where useful, explicit timeouts, and bounded retry behavior.

## 9. Shared context

Rich scenes need more than independent widgets. They need a controlled way to share context.

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

Wayfarer may establish the current latitude/longitude and timestamp. Weather can resolve conditions for that point. Immich can search for recent or historical photographs within a configured radius. The scene can then compose all of those outputs.

The context mechanism should remain generic enough for other future relationships, but it should not become a speculative global event framework before real use cases require it.

## 10. Data collection and caching

Rendering should not normally block on external API calls.

Preferred pattern:

```text
External provider
       |
       v
Collector / refresh job
       |
       v
Local cached state
       |
       v
Scene renderer
```

Refresh intervals should be configurable by source/provider where appropriate.

Examples:

- weather: periodic refresh
- news/RSS: periodic refresh
- Immich: periodic or event-driven refresh later
- Wayfarer live state: shorter polling interval or WebSocket/event mechanism where supported

If a provider is unavailable, PostcardScene should use the most recent valid cached state where sensible and expose stale/error state without breaking unrelated scene content.

The exact cache implementation is still open.

## 11. Live updates

Some scenes benefit from live changes without full-page reloads, especially Wayfarer live-location views.

The likely direction is a lightweight WebSocket path using Flask-Sock when real-time server-to-renderer updates are required.

The system should not introduce a heavier Socket.IO/event infrastructure unless its features are actually needed.

## 12. Scheduling

Scheduling has two related but distinct responsibilities.

### Display operating schedule

Controls when the physical panel should be awake.

Examples:

- daily on/off times
- different weekday/weekend schedules
- multiple active/off periods in one day
- temporary overrides

All normal schedule settings should be editable from the web UI.

### Content scheduling

Controls which sequence or scenes are appropriate at a given time.

Examples:

```text
Morning
Weather + news
Photos

Day
Photos
Wayfarer
Video

Evening
Photos
Travel scenes
Wayfarer Live
News
```

Scenes may also have conditions such as:

- Wayfarer live location is available
- recent Immich uploads exist
- a weather condition is present
- source data is available/fresh

The exact scheduling library/implementation is not frozen. Scheduling logic belongs to the application/player domain, not to Flask request handlers.

## 13. Display power management

Stopping playback is not sufficient. During configured sleep periods the application should attempt to put the physical panel into standby so it is neither a night-time light source nor needlessly active.

Power control should be abstracted behind a small display controller.

Preferred methods:

1. **HDMI-CEC** for TVs that support reliable CEC standby/wake.
2. **DDC/CI** for compatible monitors.
3. **DRM/KMS / HDMI signal control** as a fallback so the panel sees no active video signal and can enter power saving.

The settings UI should allow:

- automatic capability detection where practical
- preferred power-control backend
- fallback backend
- wake delay/handshake delay
- test power on/off actions
- schedule configuration

The host computer remains on while the display sleeps so PostcardScene can continue serving its web UI, indexing media, refreshing data, and waking the display later.

## 14. Burn-in and static-content protection

Panel protection is separate from scheduled sleep.

Potential configurable protections include:

- maximum dwell time for static web/dashboard scenes
- optional subtle position/pixel shifting for appropriate static content
- avoidance of permanent UI overlays
- renderer/player watchdog
- blanking if the renderer stalls
- eventual standby if a severe stall persists

Ordinary changing photography should not be subjected to distracting movement solely for burn-in prevention.

## 15. Web/control application

The web interface should provide purpose-built management pages rather than expose raw database/admin structures as the product interface.

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

Configuration should eventually cover:

- media directories/network sources
- Immich/Wayfarer/provider credentials
- source refresh intervals
- scene layouts/widgets
- sequence timing/order
- image/video behavior
- transitions
- display power and fallback methods
- burn-in safeguards
- operating schedules
- renderer/system status

The frontend should remain simple initially. Server-rendered Flask/Jinja pages with normal JavaScript are sufficient until a concrete interaction justifies something more. HTMX is an option, not a current requirement.

## 16. Authentication and permissions

Initial authentication should be simple and appropriate to a self-hosted appliance.

Expected initial roles:

- administrator
- possibly operator/viewer later

Do not implement a complex permission graph without need.

Use secure password hashing, session protection, CSRF protection for state-changing forms, and normal web security practices.

Credentials for external services must not be exposed in logs or unnecessarily returned to the client.

The exact credential-at-rest mechanism remains open and should be decided before storing sensitive long-lived integration credentials in production.

## 17. Privilege boundaries

The normal Flask service must not run with broad root privileges.

Operations that may need elevated access include:

- controlled SMB/NFS mount changes
- some display/hardware operations
- service/system integration

Where elevation is necessary, prefer a narrowly scoped helper or system service that only exposes the exact allowed operations with strict argument validation.

## 18. Reliability and unattended operation

PostcardScene is expected to run for long periods without supervision.

The architecture must tolerate:

- service restart
- host reboot
- display disconnect/reconnect
- NAS temporarily offline
- internet outage
- individual API outage
- Chromium crash
- mpv crash
- malformed media
- renderer stall
- missing files

The control plane should remain reachable whenever the host itself is healthy.

systemd should supervise long-running services and restart failed runtime processes according to sensible policies.

## 19. Deployment direction

Initial deployment target:

```text
Linux host
  |
  +-- postcardscene-web.service
  +-- postcardscene-player.service
  +-- Chromium
  +-- mpv
  +-- SQLite database
  +-- local/cache directories
  +-- /mnt/... SMB/NFS media mounts
```

Docker is not required for the initial design. Native Linux/systemd integration is preferable because PostcardScene needs direct access to displays, media mounts, hardware interfaces, and local playback processes.

## 20. Open architectural decisions

The following are intentionally unresolved until implementation reaches them:

1. Exact control-plane/player IPC mechanism: Unix socket, localhost HTTP/WebSocket, another narrow local protocol, or a combination.
2. Whether all images are rendered in Chromium or whether some image modes use a dedicated rendering path.
3. Exact local cache implementation and cache invalidation strategy.
4. Exact credential-at-rest strategy.
5. Exact Wayfarer native API endpoints/authentication and whether Wayfarer provides a dedicated display-mode page.
6. Exact persistence representation for scene layouts: normalized database models, JSON scene definitions, or a hybrid.
7. Whether a formal plugin/provider registration system becomes worthwhile.
8. Whether HTMX or another small frontend enhancement is justified after the basic Flask/Jinja interface exists.
9. Exact scheduling library/mechanism.

These are implementation decisions, not blockers for the foundation. They should be resolved when a concrete milestone requires them and documented at that time.
