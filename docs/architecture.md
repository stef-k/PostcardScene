# Architecture

This document captures the architectural direction for PostcardScene. It preserves decisions that should constrain implementation while leaving genuinely unresolved choices open until the issue that owns them has enough evidence to decide.

## Product shape

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

## Design principles

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

## Technology direction

Initial application direction:

```text
Python
Flask
Waitress 3.x             # sole production WSGI server
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

### Why Flask

PostcardScene needs a proper web settings/control surface, authentication, forms, persistence, and APIs, but the web layer is not the main application runtime.

Flask plus a small set of mature libraries provides the required control-plane capabilities without making the rest of the application conform to a larger full-stack framework.

The project should not become a collection of thin Flask extensions. Ordinary Python should be used where framework integration is unnecessary.

### Why SQLite initially

Users, settings, schedules, source definitions, scene definitions, media catalog records, and other application state do not initially require a separate database server.

SQLite keeps installation and recovery simple. Original media remains in filesystems or external services rather than in the database.

## High-level architecture

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
#122 freezes `postcardscene-runtime.service`, weak graphics startup ordering,
shared installed `/etc/postcardscene/config.py` consumption, and journald lifecycle
logging; see the [installed service contract](operations/runtime.md#installed-runtime-service-122).
#125 packages the independent [web unit](operations/runtime.md#installed-web-service-125)
with the #131 entry point and identity, bounded supervision and restricted filesystem/
device authority. #26 retains installation and provisioning ownership. #124 connects one shared
output monitor with [serialized signal-power coordination](architecture/graphics-and-panel.md#shared-display-mutation-124);
physical graphics validation remains #66.

A Chromium, mpv, cataloging, source, or media failure must be recoverable without losing access to the web administration interface. Restarting the control plane should not unnecessarily destroy the active display session.

The general control-plane/runtime IPC mechanism remains intentionally open. #113
adds only the narrow local [panel status/test socket](architecture/control-plane-and-security.md#local-panel-control-113).

## Architecture map

This entry point and the six linked subsystem documents collectively form the
architecture authority. Read this overview first, then the relevant contracts
for the issue. Cross-cutting changes require every affected subsystem document;
no subsystem document is standalone authority detached from this overview.

| Subsystem contract | Detailed responsibilities |
| --- | --- |
| [Composition and catalog](architecture/composition-and-catalog.md) | Composition persistence, filesystem authority, catalog reconciliation and later provider context/cache boundaries. |
| [Rendering](architecture/rendering.md) | Image delivery/presentation, video/audio, scene rendering, untrusted web content and live-update transport. |
| [Runtime and scheduling](architecture/runtime-and-scheduling.md) | Runtime lifecycle, display-step planning, playback execution and operating schedules. |
| [Graphics and panel](architecture/graphics-and-panel.md) | Wayland/labwc, output policy, Chromium/shared surfaces, overlay/input and physical panel protection. |
| [Control plane and security](architecture/control-plane-and-security.md) | Flask management surfaces, authentication, network exposure, secrets and privilege boundaries. |
| [Installation, recovery and operations](architecture/installation-recovery-and-operations.md) | Package bootstrap, shared persistence/migrations, reliability, release/install/update, backup/restore and release evidence. |

The core model remains `Source -> Widget -> Scene -> Sequence`, with the
filesystem MediaItem catalog beneath Source. Detailed contracts belong in the
subsystem documents rather than being repeated here.

User/operator/product documents retain separate roles: [Composition](composition.md),
[Sources](sources.md), [Display hardware](display-hardware.md),
[Operations](operations.md), [UI design](ui-design.md) and [Roadmap](roadmap.md).
They are not replaced by the subsystem architecture contracts.

#139 freezes the [managed-host read-only preflight](architecture/installation-recovery-and-operations.md#managed-host-preflight-139):
ARM64 Ubuntu Server 24.04/26.04 LTS and Raspberry Pi OS/Debian 13 Trixie,
with closed distro package/seat authority. #140 implements the
[initial installer](architecture/installation-recovery-and-operations.md#managed-initial-installation-140)
with explicit bootstrap, versioned activation and real two-UID Linux software
evidence. #143 retains final published-bundle manifest/member authentication.

#161 freezes the [concrete update recovery gate](architecture/installation-recovery-and-operations.md#concrete-update-recovery-evidence-161):
`verify(selected_archive) -> immutable VerifiedBackup`, carried with the selected
path in memory and required to compare equal on later verification. Persisted
backup status is advisory; #144 forward update remains unimplemented.

## Open architectural decisions

The following are intentionally unresolved until the owning issue has enough evidence:

1. **General control-plane/runtime IPC** — Unix socket, localhost HTTP, another narrow local protocol, or a combination. #52 freezes only catalog refresh requests as coalescing SQLite tokens; general live status/playback transport remains open. #113 freezes only the panel-specific AF_UNIX status/test seam.
3. **General cached-provider storage** — exact V1 cache implementation/invalidation strategy.
4. **Credential-at-rest mechanism** — exact protection/master-key approach and recovery behavior.
5. **Wayfarer native integration** — API endpoints/authentication and whether Wayfarer provides a dedicated display-oriented page.
6. **Provider/plugin registration** — whether a formal plugin mechanism ever becomes worthwhile.
7. **Frontend enhancement** — whether HTMX or another small enhancement is justified after the basic Flask/Jinja UI exists.
9. **Graphics output and renderer integration** — Wayland/labwc session, output/hotplug policy and shared Chromium control and surface/overlay/input capability are frozen by #62–#65; physical validation remains with #66.
10. **Kiosk authenticated-session persistence** — whether V0 persists third-party web-session cookies and how that state is isolated/recovered.
11. **Release artifact format** — frozen by #138: one pure-Python wheel plus hash-locked runtime requirements, inside a future GitHub Release native archive. See the [release-input contract](architecture/installation-recovery-and-operations.md#deterministic-release-inputs-138); #140/#143 retain installer/manifest/publication ownership.
12. **Production web serving/network boundary** — frozen by #131: Waitress, loopback direct HTTP or one same-host HTTPS proxy, mandatory trusted Hosts and separate web UID/private signing authority. See the [control-plane contract](architecture/control-plane-and-security.md#production-serving-131); #125 owns supervision and #26 provisioning.
13. **Renderer live-update transport** — ordinary HTTP polling is preferred where adequate; SSE, WebSockets, or another server-push mechanism is selected only if a concrete V1/V2 requirement proves it useful.

These are deliberate implementation decisions, not reasons to invent answers early. When one is resolved, update this entry point and the affected subsystem contract in the same change that relies on the decision.
