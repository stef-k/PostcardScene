# PostcardScene

**PostcardScene** is a self-hosted ambient media and information display for Linux that brings photos, video, places, web content, and live data together in composable scenes.

The project grew from the idea of a sophisticated electronic photo frame, especially for travel photography, but is intentionally broader. A display can show a single photograph or video, pair portrait images efficiently on a landscape screen, render a live Wayfarer map, rotate through news and weather, or combine several sources into one rich scene.

A useful mental model is a **living postcard**: a place, imagery, current context, and information presented together on a wall-mounted display.

## Goals

- Run comfortably on a Raspberry Pi-class Linux device connected to a modern TV or monitor.
- Target at least 1080p, with 4K as the preferred display resolution.
- Play images and videos from local storage and network storage.
- Support SMB/NFS-backed media libraries and Immich.
- Pair consecutive portrait images side-by-side when that makes better use of a landscape display.
- Display arbitrary web content in kiosk mode, including Wayfarer maps and live timeline views.
- Support weather, news/RSS, generic HTTP/JSON data, and future data providers.
- Compose media and information into rich layouts rather than limiting playback to one full-screen item at a time.
- Schedule display operating periods and put the physical panel into standby during sleep/off hours.
- Protect OLED and other panels from static-content burn-in risks.
- Expose normal configuration through a proper web settings interface.
- Remain useful when external services or the internet are temporarily unavailable.

## Core model

PostcardScene is designed around four concepts:

```text
Source -> Widget -> Scene -> Sequence
```

- **Source** retrieves or exposes content/data: local files, NAS, Immich, Wayfarer, weather, RSS, HTTP/JSON, URLs, and similar providers.
- **Widget** presents one kind of content or data: image, video, weather, headline, map, location, photo strip, web view, text, and so on.
- **Scene** composes one or more widgets into a display layout.
- **Sequence** controls which scenes are shown, in what order, for how long, and under what conditions.

This model supports both simple playback and richer compositions. A scene might be a single full-screen photograph, two portrait images side-by-side, or a Wayfarer live map with current-location weather and recently uploaded Immich images from the same area.

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
- WebSockets, likely through Flask-Sock
- Pillow
- Chromium in kiosk mode for rich HTML/CSS/JavaScript scene rendering
- mpv for video playback
- systemd for services
- Linux SMB/NFS mounts for network storage
- HDMI-CEC, DDC/CI, and DRM/KMS display-power control where available

The web/control process and the display/player process are separate services. A browser or media failure must not take the administration interface down with it.

## Planned integrations

### Media

- Local directories
- SMB/NFS-mounted directories
- Immich
- Images
- Video

### Web and information

- Arbitrary URLs
- Wayfarer
- Weather providers
- RSS/Atom news feeds
- Generic HTTP/JSON sources

Wayfarer is expected to have a richer native integration in addition to ordinary URL display. For example, a live Wayfarer position can become shared scene context, allowing PostcardScene to show local weather and select geographically relevant Immich photos around the current location.

## Display management

Display power management is a first-class feature rather than merely stopping playback.

The application should support configurable schedules and attempt physical panel standby using the best available method:

1. HDMI-CEC for compatible TVs.
2. DDC/CI for compatible monitors.
3. HDMI/DRM signal shutdown as a fallback.

The Raspberry Pi or host remains running so the web interface, scheduler, media indexing, and integrations continue to work while the panel sleeps.

Panel protection will also include safeguards for static content, such as configurable maximum dwell times, optional movement/pixel shifting where appropriate, and a watchdog that blanks or powers down the display if the renderer stalls on one frame.

## Documentation

- [Architecture](docs/architecture.md)
- [Roadmap](docs/roadmap.md)
- [Agent/development guidance](AGENTS.md)

## Status

PostcardScene is currently in the architecture and foundation stage. The first implementation milestone is intentionally smaller than the complete vision: establish reliable local/network media playback, basic scenes and sequences, web control, scheduling, and display power management before adding the richer contextual integrations.
