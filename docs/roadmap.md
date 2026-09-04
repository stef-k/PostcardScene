# Roadmap

This roadmap keeps the first implementation deliberately smaller than the complete PostcardScene vision while preserving the architecture needed for richer scenes later.

The phases describe capability boundaries rather than fixed release dates.

## Foundation / V0

Goal: prove that PostcardScene can operate reliably as an unattended media-display appliance before expanding into rich external-data integrations.

### Application foundation

- Python project/package structure
- Flask web/control application
- SQLAlchemy + SQLite persistence
- database migrations
- local authentication
- settings UI foundation
- separate web/control and player services
- systemd service definitions/install direction
- status/health information sufficient to diagnose player failures

### Core domain

Implement the foundational model from the beginning:

```text
Source -> Widget -> Scene -> Sequence
```

V0 only needs a small number of concrete implementations, but media playback should not bypass this model with a separate throwaway slideshow architecture.

### Media sources

- local directory source
- mounted network-directory source
- SMB/NFS documented as normal Linux mounts
- source availability/error handling

### Image playback

- landscape images
- portrait images
- consecutive portrait-image pairing in a landscape layout
- fit/fill behavior
- configurable image duration
- sequence order/shuffle basics
- basic transitions

### Video playback

- mpv integration
- local/network video files
- play-full-duration behavior
- controlled skip/failure behavior
- recovery from player failure

### Basic web scenes

- arbitrary URL scene/widget
- Chromium kiosk integration
- configurable duration
- basic renderer supervision/restart

This is sufficient to display a Wayfarer URL before native Wayfarer integration exists.

### Basic scenes and sequences

- single-content scene
- portrait-pair scene
- simple multi-region/layout capability
- scene duration
- ordered/shuffled sequences
- manual next/previous/pause/resume controls where appropriate

The scene representation should leave room for multiple widgets without requiring the full future scene editor in V0.

### Operating schedule

- configurable display on/off periods
- support for more than one active/off span where practical
- weekday/weekend or day-specific rules
- temporary/manual override
- scheduling independent of browser requests

### Display power

- display-power abstraction
- configurable backend selection
- HDMI-CEC backend
- DDC/CI backend
- DRM/KMS or HDMI-signal fallback where supported
- configurable wake/handshake delay
- test on/off controls from settings
- safe behavior when a preferred backend is unavailable

### Panel protection

- maximum static-content dwell safeguards where relevant
- renderer/player watchdog
- safe blanking when playback stalls
- fallback toward panel standby for prolonged severe failure
- settings for the implemented protection behavior

### V0 completion criteria

V0 is successful when a Raspberry Pi-class Linux system can boot unattended, start PostcardScene, play local/NAS images and video, display URLs, compose at least basic scenes, obey a configured operating schedule, sleep/wake the connected display reliably, and recover from ordinary renderer/player failures without losing the management interface.

Do not postpone reliability work merely to add additional content providers.

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

- provider refresh jobs
- local cached state
- source freshness/error metadata
- rendering from cached state rather than blocking directly on normal remote requests

### Live renderer updates

Where justified, add a lightweight WebSocket path, likely Flask-Sock, so a running Chromium scene can receive updated state without full reloads.

Do not introduce heavier messaging infrastructure unless concrete requirements exceed this model.

### V1 completion criteria

V1 is successful when PostcardScene can create polished mixed-content scenes from local/Immich photography plus current information such as weather and news, while continuing to operate gracefully when remote sources are slow or unavailable.

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

Visual richness must not compromise reliability or panel-safety behavior.

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
- mobile-friendly remote controls
- richer rule/condition systems

These are not foundation requirements.

## Roadmap rule

When choosing between adding another provider and making the existing display runtime more reliable, prefer reliability until the foundation completion criteria are satisfied.

The project should grow outward from a stable display appliance, not inward from a large collection of integrations.
