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
- Alembic (direct integration with a thin Flask CLI adapter)
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

The shared session foundation targets Raspberry Pi 4/5 ARM64 on **both Ubuntu
Server LTS and Raspberry Pi OS 64-bit**, using native Wayland/labwc. Versioned
appliance configuration, service/PAM templates and a bounded Python readiness
probe are implemented in #62. See [appliance operations](docs/operations.md) for
provisioning ownership and validation limits. #63 implements the shared one-HDMI
mode policy, bounded hotplug polling seam and safe diagnostics; #124 runs one
monitor in RuntimeHost, serialized with intentional signal power operations. #64 provides one packaging-neutral isolated Chromium
lifecycle/navigation controller; image/web adapters and runtime playback are not
yet connected. Managed installation and physical support evidence remain with
their owning issues.

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

## Development

Development requires Python >=3.11 and
[uv](https://docs.astral.sh/uv/getting-started/installation/).
See [AGENTS.md](AGENTS.md#project-local-development-tooling) for the canonical
development commands and tooling workflow.

### Control shell development

After `uv sync --locked` and the database/authentication setup below, start the
local control interface with:

```bash
uv run flask --app postcardscene.web:create_app run --host 127.0.0.1 --no-debug
```

Open <http://127.0.0.1:5000/> and log in. Authentication protects Overview, Settings, Sources, Widgets, Scenes, Sequences, Schedule and Display. Overview shows the application
version, database/schema health and unavailable runtime status; playback controls
are not implemented.
The command starts only the web process. Stop it with Ctrl-C.

The Flask development server is for local development only. It is not the managed
production appliance server. Issues #29 and #26 own the production WSGI server,
bind address, HTTP/HTTPS and reverse-proxy deployment policy.

`create_app(config=None)` loads Flask defaults (debug/testing off, no session
secret), then an optional Python configuration file named by the
`POSTCARDSCENE_CONFIG` environment variable, then explicit mapping overrides for
tests or a deployment entrypoint. Use an absolute path to an operator-owned file
outside the checkout; a specified missing/unreadable file fails startup. Python
configuration is trusted executable host configuration, never web input. Keep
secrets out of source control; signing authority comes exclusively from the
protected file described below, overriding any configured `SECRET_KEY`.

Flask configuration such as `TRUSTED_HOSTS` can be supplied through that file or
mapping. `SERVER_NAME` is not a bind address or a Host allowlist. The future WSGI
server owns listening configuration; the factory returns a standard Flask WSGI
application and does not bind a socket. Forwarded headers are not trusted through
proxy middleware by default; #29 must define trusted proxy topology before adding
such middleware. Keep debug/testing disabled in deployment configuration. Normal
error responses omit exception details; protected server logs remain diagnostic.

### Runtime development

After `uv sync --locked`, run `uv run postcardscene-runtime`. The packaged
`postcardscene-runtime` executable starts only the runtime host and waits until
Ctrl-C (SIGINT) or SIGTERM requests cooperative shutdown. Normal exit is zero;
fatal failure exits nonzero with fixed, sanitized stdlib lifecycle diagnostics.
The packaged [installed runtime service](docs/operations.md#installed-runtime-service-122)
uses the shared trusted `/etc/postcardscene/config.py` path and journald logging;
#26 owns installation and enablement.

Initialize/migrate the shared database first, as described below. Web and runtime
read `DATABASE_PATH` and `MEDIA_ALLOWED_ROOTS` from the same optional operator-owned
Python file selected by `POSTCARDSCENE_CONFIG`; the runtime loader does not import
Flask. A configured missing/unreadable file or incompatible database fails runtime
startup. The runtime performs explicitly requested catalog refreshes and monitors the
shared graphics output. Trusted configuration optionally enables panel power;
see [panel runtime operations](docs/operations.md#live-panel-runtime-and-local-control-113).
Playback, automatic scans and scheduling are not connected.
Web and runtime restart independently; Overview still reports runtime unavailable
until a later issue chooses and connects IPC.

### Persistence development

The shared `postcardscene.persistence` module uses SQLAlchemy without Flask
contexts. Alembic migrations are packaged with the application; the thin Flask
`db` command group uses that same persistence layer (no Flask-SQLAlchemy or
Flask-Migrate extension is needed).

`DATABASE_PATH` defaults to `/var/lib/postcardscene/postcardscene.sqlite3`. It must
be an absolute file path on local storage, never an SMB/NFS filesystem or an
in-memory database. The installer/operator owns directory creation, permissions
and selection of the shared path for both services. Keep the parent directory
private to the service account; SQLite also needs permission to create WAL/SHM
sidecars there. Neither app construction nor normal database access creates a
missing file or directory. Tests supply isolated temporary paths.

For development, provision a private directory outside the checkout and put
`DATABASE_PATH = "/absolute/path/to/state/postcardscene.sqlite3"` in the trusted
Python configuration file selected by `POSTCARDSCENE_CONFIG`. Then run:

```bash
uv run flask --app postcardscene.web:create_app db upgrade
uv run flask --app postcardscene.web:create_app db check
```

`upgrade` explicitly creates an empty database or migrates a recognized revision
to the packaged head; repeated upgrades are safe. Stop all database users before
migration. It refuses unrelated, unversioned nonempty, corrupt and unknown-revision
databases. There is no automatic startup migration, `create_all`, stamping,
downgrade or repair path. App construction remains database-lazy so the migration CLI is available before
initialization; each application
transaction checks schema compatibility before yielding a session.

`check` performs schema/WAL, SQLite `quick_check` and foreign-key checks and emits
JSON application/version/file/schema identity only on success. It is a diagnostic
primitive, not a backup or proof of semantic correctness. Failures exit nonzero;
no check attempts repair. Python callers receive `DatabaseError` for compatibility
or integrity failures and SQLAlchemy exceptions for storage/locking failures.
CLI errors omit raw database details. Keep underlying exceptions in protected
operator diagnostics, not web responses.

Future models inherit `postcardscene.persistence.Base`. Use
`with database.transaction() as session:` for a short unit of work: success
commits, exceptions roll back, and the session always closes. Autoflush is off;
flush explicitly before queries that need pending changes. Commit still flushes,
and objects expire on commit. Do not share sessions across requests, threads or
processes, or retain them during network/media work. There is no request-teardown
commit or global scoped session. Flask callers obtain the process-local database
from `app.extensions["postcardscene.database"]`; runtime callers instantiate
`Database(absolute_path)` directly. Dispose the engine when its owner shuts down.

When a later issue changes models, import them into the migration metadata and
run `db revision -m "description"` in a writable development checkout against an
up-to-date disposable database. Review the generated Alembic script, including
SQLite batch operations and data preservation, format/lint it, update
`SCHEMA_REVISION` to the new single head, and test an explicit upgrade. The initial
baseline contains only Alembic revision bookkeeping and the SQLite application
ID; it does not pre-create future domain tables. Production uses packaged
migrations, never autogeneration. Forward production updates and restore require
the recovery contract still owned by #26/#27; replacing application files or
matching the schema revision alone does not establish safe rollback.

### Source and Widget domain

When updating from the application foundation, stop database users and run
`db upgrade` explicitly. Migration `0004_source_widget` adds Source/Widget tables
and preserves administrator/settings state. Filesystem Sources can be managed
through the authenticated Sources area described below.
Ordinary Python callers use `postcardscene.domain` create/update/get/list/remove
functions within the existing `Database.transaction()` context. Updates supply
all fields and replace configuration as a whole; invalid input raises
`DomainError` before mutation. See [architecture](docs/architecture/composition-and-catalog.md#source-and-widget-persistence)
for kinds, limits, relationships and the configuration trust boundary.

### Scene domain

After stopping database users, explicitly run `db upgrade` to apply `0005_scene`.
Existing administrator, settings, Sources and Widgets are preserved. Scene APIs
in `postcardscene.domain` work inside `Database.transaction()` without Flask:
`create_scene`, `get_scene`, `list_scenes`, `update_scene`, `remove_scene`, and
`list_scene_placements`. The authenticated Scenes area edits V0 `single` Scenes;
rendering is not connected to these forms.

Create/update takes a complete list of `(region, widget_id)` pairs; the domain
derives canonical positions for `single` (`main`), `split_vertical` (`left`,
`right`), or `split_horizontal` (`top`, `bottom`). For example, use
`placements=[("main", pair_widget_id)]` with `layout="single"` for an existing
`portrait_image_pair` Widget. Update supplies all Scene fields and validates the
replacement before mutation. `duration_seconds` is `None` for no fixed dwell or
an integer 1–86400; `enabled` is a boolean.

Referenced Widgets cannot be deleted. Unreferenced Scene removal deletes only placements,
preserving Widgets and Sources; disable preserves references. See
[Scene architecture](docs/architecture/composition-and-catalog.md#scene) for the complete contract.

### Composition management

Use **Widgets** to create/edit Image, Portrait pair, Video and Web view Widgets
from existing compatible Sources, then **Scenes** to configure one Widget in a
V0 `single` Scene. Forms preserve restrictive deletion and non-destructive disable
semantics. Existing split Scenes are listed as non-executable in V0 and cannot be
converted or edited here. See the [composition guide](docs/composition.md) for
options, duration precedence and lifecycle behavior. Use **Sequences** to manage
ordered/shuffle occurrence lists with Add/Remove/Move controls, then **Settings**
to select an active Sequence or None / Idle and set fallback dwell. Repeated Scene
occurrences remain distinct; existing split occurrences must be explicitly removed
or replaced before saving an edited Sequence. Runtime controls and actual playback
integration remain with #92/#93.

### Filesystem media catalog

Stop database users and run the explicit `db upgrade` command to apply
`0007_media_catalog`; existing configuration is preserved. The catalog remains
beneath Source, storing relative identity and basic image/video freshness without
copying originals. Pillow supplies header-only presentation dimensions/orientation;
video duration is reserved for later playback work.

`postcardscene.catalog_reconciliation.reconcile_filesystem_source` performs one
bounded local/mounted reconciliation outside Flask requests. Only a complete
presence scan removes missing items; outages/partial scans preserve unseen rows.
Mounted metadata reads run in disposable child processes. Bad images remain present
with metadata errors, and unchanged ready images are not reopened. Source disable
preserves the catalog. Scan generations/results and bounded query/count functions
are available for runtime/UI work; no automatic scan scheduling is added.

Migration `0008_catalog_requests` preserves existing state and adds durable refresh
request counters. Configure `MEDIA_ALLOWED_ROOTS = ["/absolute/media/root"]` in the
same trusted Python file used by both processes. It defaults to an empty tuple
(no filesystem authority), accepts only a list/tuple of absolute host roots via
`PathPolicy`, and is never Source JSON or an ordinary web setting. #26 owns managed
installation provisioning; the Sources UI consumes this policy read-only.

Ordinary Python callers can use
`postcardscene.catalog_requests.request_catalog_reconciliation(database, source_id)`
to request refresh of an existing enabled filesystem Source. This writes only the
DB and returns an incremented coalescing token. The runtime polls one request per
second on one dedicated thread and invokes the existing reconciliation operation.
Ready/unavailable/error results consume the captured token without automatic retry;
a newer request remains pending. Shutdown cancellation/process death leaves work
pending for restart. Read `MediaCatalogState.refresh_pending`, `interrupted`, result
and attempt/success timestamps for queued/active/interrupted and last-result health.

Use `domain.update_source` inside `database.transaction(write=True)` for Source
edits: changing kind/path/recursive supersedes scans/requests and clears derived
items/freshness; disabling supersedes work while preserving items and last success.
Renaming/re-enabling preserves catalog knowledge. An enabled edited Source needs a
new explicit request after saving. Deletion keeps the existing composition rule
and cascades Source-owned catalog/request state.

Normal shutdown cancels and joins the catalog thread within five seconds. Mounted
I/O retains disposable-process isolation. Python cannot interrupt a blocked local
kernel filesystem call; if cleanup exceeds the bound, the runtime exits nonzero
with a safe diagnostic rather than claiming clean shutdown. The worker thread is
daemonic only to preserve that process-exit bound, not another service.

Catalog state is regenerable. Whole-database backups may contain it, but restored
catalog freshness must be re-established from external Sources by reconciliation.
See [catalog architecture](docs/architecture/composition-and-catalog.md#persistent-catalog-and-reconciliation-30)
for API, failure and metadata limitations.

### Sources control UI

Open **Sources** after logging in to manage local and already-mounted NFS/SMB
Sources, queue runtime refreshes, and inspect persisted catalog health/counts.
Choose **Add web Source** to manage HTTP/HTTPS display URLs (including LAN
services) without network probes or a media catalog. Web configuration/resolution
is implemented; rendering and composition execution remain #83/#8.
See the [Media Sources guide](docs/sources.md) for allowed-root setup, editing,
enabling/disabling, refresh and outage semantics, and deletion.

### Sequence domain

Stop database users and explicitly run `db upgrade` to apply `0006_sequence`.
Existing administrator/settings and Source/Widget/Scene data are preserved.
Within `Database.transaction()`, `postcardscene.domain` exposes `create_sequence`,
`get_sequence`, `list_sequences`, `update_sequence`, `remove_sequence`, and
`list_sequence_memberships`; no Flask initialization is needed.

Create/update supplies `name`, `mode` (`ordered` or `shuffle`) and a complete
nonempty ordered `memberships=[(scene_id, duration_override_seconds), ...]` list.
Create defaults `enabled=True`; update requires all fields including `enabled`.
Duplicate Scene occurrences are allowed. Positions are derived from list order
and remain unchanged by shuffle mode. Each occurrence has a stable persisted ID
until deliberate membership replacement, which may assign new IDs.
Overrides accept `None` or integer 1–86400, excluding booleans; Scene durations
remain unchanged. Invalid updates raise `DomainError` before mutation.

Disabled Scenes remain valid references; disabling either object preserves
membership configuration. Referenced Scenes cannot be deleted. Sequence deletion
removes only its memberships, preserving Scenes, Widgets and Sources, and clears
active selection to Idle if applicable. The authenticated Sequence editor uses
these operations; playback integration remains #93 and operating schedules #9. See [Sequence architecture](docs/architecture/composition-and-catalog.md#sequence).

### Application settings and status

After stopping database users, run the explicit `db upgrade` command when updating
from the authentication foundation. Migration `0003_application_settings` creates
one typed settings row with application timezone `UTC`; repeated upgrades preserve
saved settings and the administrator. Startup never creates or repairs settings.

Log in and open **Settings** to save an IANA timezone such as `Europe/Athens` or
`UTC`. Names are validated against Python `zoneinfo` and the host timezone database;
invalid input leaves the previous value unchanged and shows form feedback. Keep
the host timezone database installed and current. This stores appliance
configuration only: it does not change the host clock. **Schedule** uses this
timezone for weekly active windows and bounded temporary active/sleep overrides;
see the [schedule operator guide](docs/operations.md#weekly-schedule-and-temporary-overrides).
Schedule shows configured intent; live playback/panel integration remains pending. The dark/light toggle remains a browser-local presentation choice.

**Overview** shows the installed application version and the exact schema revision
only after the shared database health check succeeds. An unsuccessful check shows
**Needs attention** with an unverified revision, without raw errors or paths; use
`db check` on the host for diagnosis. Authentication itself still requires a
compatible, readable database; the dashboard is not an out-of-band recovery tool.
Runtime/player status is **Unavailable / Not yet connected** until #17 establishes
its real contract. No runtime service is probed or controlled by these pages.

### Local authentication and recovery

Run setup as the unprivileged service account (your own user for development).
Host access as that account, or root acting as that account, is the recovery
authority: protect its database, configuration and signing-key paths from other
users. There is one administrator, no registration, roles or web recovery.

Provision an owner-only directory (`0700`) outside the checkout and configure
`SESSION_SECRET_PATH = "/absolute/path/to/private/session.key"` alongside
`DATABASE_PATH`. The default key path is `/var/lib/postcardscene/session.key`.
The key's immediate parent must be owned by the effective user with no group/other
permissions; keep ancestor directories under trusted host control. Then run:

```bash
uv run flask --app postcardscene.web:create_app auth init-secret
uv run flask --app postcardscene.web:create_app db upgrade
uv run flask --app postcardscene.web:create_app auth create-admin --username admin
```

The password is prompted twice without echo, never accepted as a command-line
argument. Use 12–128 characters; usernames are case-sensitive, 1–64 characters,
with no outer spaces. Bootstrap refuses to replace an existing administrator.
Passwords use Werkzeug scrypt hashes in SQLite. No manual database editing is
needed. Stop database users before explicit migration; restart web after setup.

For a forgotten password, use the same trusted configuration and service account:

```bash
uv run flask --app postcardscene.web:create_app auth reset-password --username admin
```

Reset requires the existing username and a hidden confirmed new password. It
revokes all existing login sessions, including copied cookies, without restarting
web. The **Log out all sessions** POST action also revokes all browser sessions
for this single administrator. Login and logout require CSRF tokens. Login always
returns to Overview; supplied redirect destinations are ignored.

`auth init-secret` creates a random 32-byte key with mode `0600`; repeated calls
validate existing authority without replacing it. Web startup reads it without
creating it. Missing authority leaves setup/database commands available but web
returns 503; unsafe permissions, symlinks or malformed keys fail startup. Keep this
sensitive installation state across process restarts. Do not delete/regenerate it
as routine recovery; intentional replacement requires stopping/restarting every
web process and invalidates signed sessions. Backup/recovery integration is owned
by #27/#29. This signing file is not a provider-credential encryption/master key.

Sessions use Flask-Login strong protection, browser-session cookies (no remember
me), a 12-hour signed-cookie age limit, HttpOnly, SameSite=Lax, no Domain, and
`Cache-Control: no-store` for dynamic responses. Sessions survive web reconstruction
with the same database/key until expiry or revocation; browser restore behavior
may preserve browser-session cookies. Configure `SESSION_COOKIE_SECURE = True`
when serving HTTPS. It defaults to false solely for the documented loopback HTTP
development server. Do not expose this development transport remotely. No proxy
headers are trusted automatically. #29/#26 retain the final production server,
bind/Host/proxy/TLS and cookie transport policy; #29 also owns bounded login-abuse
protection, broader headers and provider credential-at-rest architecture.

## Documentation

- [Appliance operations](docs/operations.md)
- [Media Sources guide](docs/sources.md)
- [Architecture](docs/architecture.md)
- [Roadmap](docs/roadmap.md)
- [UI design](docs/ui-design.md)
- [Display hardware guidance](docs/display-hardware.md)
- [Agent/development guidance](AGENTS.md)

The operations guide grows with implemented operating contracts; managed installation remains planned.

## License

PostcardScene is licensed under the [MIT License](LICENSE).

## Status

PostcardScene is currently in the architecture and Foundation/V0 stage. Codex coding agents will implement hardened GitHub issues. The first milestone deliberately proves the appliance foundation—local/network media, cataloging, basic scenes, scheduling, web rendering, graphics/display control, security, installation, diagnostics, backup/recovery, and documentation—before the richer V1/V2 integrations are added.
