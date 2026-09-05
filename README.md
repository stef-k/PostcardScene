# PostcardScene

**PostcardScene** is a self-hosted ambient media and information display for Linux that brings photos, video, places, web content, and live data together in composable scenes.

The project grew from the idea of a sophisticated electronic photo frame, especially for travel photography, but is intentionally broader. A display can show a single photograph or video, pair portrait images efficiently on a landscape screen, render a live Wayfarer map, rotate through news and weather, or combine several sources into one rich scene.

A useful mental model is a **living postcard**: a place, imagery, current context, and information presented together on a wall-mounted display.

## Goals

- Run comfortably on a Raspberry Pi-class Linux device connected to a modern TV or monitor.
- Target at least 1080p, with a physically validated 4K path as the preferred display target.
- Play high-quality images and videos from local storage and mounted network storage.
- Maintain a bounded media catalog so large local/NAS libraries do not need to be rescanned for every scene transition.
- Pair consecutive portrait images side-by-side when that makes better use of a landscape display.
- Display supported web content in an isolated Chromium kiosk context, including Wayfarer maps and live timeline/share views.
- Support weather, news/RSS, generic HTTP/JSON data, Immich, Wayfarer, and future data providers in later milestones.
- Compose media and information into rich layouts rather than limiting playback to one full-screen item at a time.
- Schedule display operating periods and put the physical panel into standby during sleep/off hours.
- Protect OLED and other panels from static-content and stalled-renderer risks.
- Expose normal configuration through an authenticated, responsive web settings interface.
- Remain useful when NAS, displays, individual media files, external services, or the internet are temporarily unavailable.
- Be installable, diagnosable, upgradable, backed up, and recoverable as a Linux appliance rather than only runnable from a source checkout.

## Core model

PostcardScene is designed around four composition concepts:

```text
Source -> Widget -> Scene -> Sequence
```

- **Source** retrieves or exposes content/data: local files, mounted NAS paths, Immich, Wayfarer, weather, RSS, HTTP/JSON, URLs, and similar providers.
- **Widget** presents one kind of content or data: image, video, weather, headline, map, location, photo strip, web view, text, and so on.
- **Scene** composes one or more widgets into a display layout.
- **Sequence** controls which scenes are shown, in what order, for how long, and under what conditions.

Filesystem sources may additionally maintain normalized `MediaItem` catalog records for efficient indexing/selection. That catalog is a source/runtime data layer and does not replace the four-part composition model.

This supports both simple playback and richer compositions. A scene might be a single full-screen photograph, two portrait images side-by-side, or later a Wayfarer live map with current-location weather and recently uploaded Immich images from the same area.

## Initial technology direction

The current design direction is deliberately lightweight on the control side while leaving room for a rich display experience:

- Python
- Flask
- SQLAlchemy
- SQLite
- Alembic / Flask-Migrate
- Flask-Login
- Flask-WTF
- `requests`
- `feedparser`
- Pillow
- Chromium in kiosk mode for rich HTML/CSS/JavaScript scene rendering
- mpv for video/audio playback
- systemd for services and unattended operation
- Linux SMB/NFS mounts for network storage
- HDMI-CEC, DDC/CI, and DRM/KMS display-power control where available

The web/control process and the long-running runtime/player process are separate services. Long-running source reconciliation/scheduling belongs outside Flask requests. A browser, media, or runtime failure must not take the administration interface down with it.

Renderer state updates should use the simplest transport that satisfies the required update cadence. Ordinary HTTP polling is preferred where adequate; WebSockets, SSE, or another server-push mechanism should be introduced only when a concrete live-scene requirement justifies the extra dependency and lifecycle complexity.

## Planned integrations

### Media

- Local directories
- SMB/NFS-mounted directories
- Immich
- Images
- Video/audio

### Web and information

- Supported HTTP/HTTPS URLs
- Wayfarer
- Weather providers
- RSS/Atom news feeds
- Generic HTTP/JSON sources

Wayfarer is expected to have a richer native integration in addition to ordinary URL display. For example, a live Wayfarer position can become shared scene context, allowing PostcardScene to show local weather and select geographically relevant Immich photos around the current location.

## Display appliance behavior

PostcardScene owns more than playback. V0 includes a deliberate Linux graphics-session boundary so Chromium and mpv target the intended HDMI display without depending on a general desktop/login session. 1080p is the minimum target and the supported 4K path must be proven on representative Raspberry Pi-class ARM64 hardware before being claimed as supported.

Display power management is separate from graphics rendering. The application should support configurable schedules and attempt physical panel standby using the best available method:

1. HDMI-CEC for compatible TVs.
2. DDC/CI for compatible monitors.
3. HDMI/DRM signal shutdown as a fallback.

The host remains running while the panel sleeps so the web interface, scheduler, media catalog reconciliation, and later integrations can continue.

Panel protection also includes bounded static-content dwell, renderer/player watchdogs, safe blanking, and eventual standby behavior where appropriate.

## V0 operational boundary

The Foundation milestone is not complete merely when source code can display media. It also includes:

- reproducible CI/release artifacts and a managed native Linux installation/update path;
- systemd-supervised web/runtime services and a controlled graphics session;
- a documented security boundary for remote administration, filesystem/subprocess privileges, secrets, and arbitrary web scenes;
- a PostcardScene-state backup/verification/restore system, including recovery onto a clean replacement host;
- status/doctor-style diagnostics and bounded logs/cache/disk behavior;
- operator documentation and a tested support/hardware matrix;
- required third-party notices before a public distributable release.

External media libraries themselves are not copied into PostcardScene backups.

## Documentation

- [Architecture](docs/architecture.md)
- [Roadmap](docs/roadmap.md)
- [Display hardware guidance](docs/display-hardware.md)
- [Agent/development guidance](AGENTS.md)

A single `docs/operations.md` is expected when implementation reaches stable installation/operation workflows; it should not be split prematurely.

## License

PostcardScene is licensed under the [MIT License](LICENSE).

## Status

PostcardScene is currently in the architecture and Foundation/V0 stage. Codex coding agents will implement hardened GitHub issues. The first milestone deliberately proves the appliance foundation—local/network media, cataloging, basic scenes, scheduling, web rendering, graphics/display control, security, installation, diagnostics, backup/recovery, and documentation—before the richer V1/V2 integrations are added.