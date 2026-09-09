# Control plane and security

Flask management surfaces, authentication, network exposure, secrets and privilege boundaries.

Read the [architecture entry point and map](../architecture.md) first. Together,
the overview and linked subsystem documents form the architecture authority.

Filesystem authority is in [Composition and catalog](composition-and-catalog.md#filesystem-source-contract-22); untrusted web URL/profile semantics are in [Rendering](rendering.md#web-content-isolation).

## Web/control application

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
See [UI design](../ui-design.md) for the visual, accessibility and asset policies,
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
Flask Host configuration without requiring production settings for development or
CLI use. #131's production entry point validates serving authority, passes one
already-loaded operator snapshot to the factory, then applies transport overrides.
Server binding remains outside the factory; no forwarded-header middleware is
installed in Flask. The shell has a lazy shared
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

Issue #25 adds the authenticated filesystem `sources` blueprint: list,
create/edit, POST refresh and POST delete, all mutations CSRF-protected. Forms
validate through `PathPolicy(MEDIA_ALLOWED_ROOTS)` and #22 before short Source
write transactions; normalized configuration goes through the domain seam.
Enabled creation, authority edits and re-enable queue #52 requests after commit;
queue failure preserves the saved Source and offers manual retry. Rename does
not queue; disable and authority invalidation retain #52 semantics. Empty/invalid
roots leave the DB-only list usable but prevent filesystem form saves. Roots are read-only
host guidance; there are no mount controls, credentials or filesystem browser.

A small explicit view model derives health from persisted Source/catalog state,
prioritizing disabled, interrupted, queued, then last result. It shows older
result/freshness, timestamps in the Settings timezone, and #30 grouped counts
without materializing MediaItems or touching storage. Retained counts are not
current availability claims; only a completed authoritative presence scan means
Ready. Database failures produce sanitized 503 feedback. Deletion uses the
existing restrictive domain/FK lifecycle. Schema remains `0008_catalog_requests`.

Issue #82 extends Sources with **Add web Source** and a dedicated structured
name/kind/URL/enabled form, using the shared web semantic validator before saving.
Edit selects the form from the stored Source kind; web/filesystem conversion is
not offered. Login and CSRF protect every page/mutation. URLs are shown only in
the authenticated Sources area. Web saves require no media roots or network probe,
create no Widgets/catalog/refresh requests, and invalid input leaves state intact.
Web cards omit filesystem health/counts/refresh; restrictive deletion is shared.
Filesystem forms, refresh/invalidation and health retain #25's behavior.

Issue #90 adds authenticated `widgets` and `scenes` blueprints with structured
Flask-WTF forms and POST/CSRF mutations. Widget kinds are fixed at creation;
image/pair/video/web forms normalize configuration through #56/#71/#82 validators.
Source compatibility is rechecked inside the short writer transaction, including
disabled compatible Sources. Scene editing replaces one `main` placement through
the domain with layout fixed to `single`; existing splits remain listed as
non-executable in V0, reject edits, and retain restrictive deletion. Disabling
preserves references and related state. These DB-only pages perform no content
probes, catalog requests, rendering, Sequence editing or runtime commands and add
no schema. See the [composition guide](../composition.md) for the operator workflow.

Issue #91 adds authenticated Sequence forms with complete domain membership
replacement, repeated occurrences, and server-rendered draft Add/Remove/Move
controls. New membership choices are single Scenes, including disabled Scenes;
existing splits stay visible and must be explicitly replaced or removed before
saving. Settings exposes explicit active Sequence/Idle and fallback dwell through
separate POST/CSRF forms using #87 typed setters. Active Sequence deletion clears
selection through the existing FK. These operations are DB-only, with no schema,
transient playback persistence or runtime IPC; #92/#93 retain controls/integration.

Issue #103 adds authenticated **Schedule** with weekly enablement/windows,
server-rendered draft Add/Remove, complete atomic Save, bounded temporary
Keep active/Sleep override and Resume schedule now. All actions are POST/CSRF;
#101 owns persistence and pure request-time evaluation. Local HH:MM conversion
happens only at the web boundary, with end 00:00 representing minute 1440.
Settings remains the timezone authority. The page labels configured intent and
local override expiry, without claiming actual runtime/panel state. Errors are
sanitized; there are no probes, runtime commands, schema or scheduler additions.
See [operations](../operations.md#weekly-schedule-and-temporary-overrides).

## Local panel control (#113)

The runtime owns `/run/postcardscene/panel-control.sock`: Linux AF_UNIX
SOCK_SEQPACKET, one JSON packet per connection, version 1, exact
`status|test_on|test_off`, request/response maximum 1024 bytes. Only fixed panel
status and outcome fields leave the process. Diagnostic actions submit #112's
five-second intent and cannot override protection. No backend arguments, paths,
schedule fields, raw tool output or arbitrary method names are accepted.

The pre-existing directory must belong to the non-root runtime UID with mode
0700 (socket 0600), or 0750 (socket 0660, directory GID). Only the runtime owner
can replace the endpoint; SO_PEERCRED permits the owner UID or the configured
directory group's primary GID. #26 owns eventual web/runtime user/group
provisioning. Invalid authority and existing endpoints fail startup; application
code never creates/chowns the parent or deletes a foreign/stale socket. Clients
have 100 ms I/O bounds and the single listener joins within five seconds.

#114 adds authenticated **Display** with atomic #110 policy replacement and
separate POST/CSRF Test wake/Test sleep actions. The ordinary-Python `panel_client`
uses the fixed path/version and three closed actions, one connection/packet per
request, 1024-byte receive bound and 100 ms connect/read/write timeouts without
retries. Invalid responses and socket failures become sanitized unavailable
feedback. Only validated fixed vocabulary reaches templates. Settings saves send
no socket command; unavailable runtime status leaves durable management usable.
Flask never imports/constructs panel hardware owners or invokes their tools.
Accepted tests mean submitted, not completed physical transitions; cleanup failure
requires runtime recovery and is never retried from Flask. General runtime/playback
IPC remains #29 authority. See [Display guidance](../operations.md#display-settings-status-and-tests).
See [wire protocol and operations](../operations.md#live-panel-runtime-and-local-control-113).

## Authentication, network exposure, and secrets

Initial authentication should be simple and appropriate to a self-hosted appliance.

Expected initial authority:

- administrator
- optional viewer/operator only when a concrete need appears

Issue #15 uses a single SQLAlchemy administrator (explicit `0002_administrator` migration), Werkzeug scrypt hashes, Flask-Login strong session protection and Flask-WTF CSRF. Logout/reset rotate its revocable login identity; all sessions are revoked.

Host CLI commands provision a protected persistent signing file and bootstrap/reset the administrator without database editing. Cookies are HttpOnly/SameSite=Lax, age-bounded to 12 hours; Secure is transport-derived in production and configurable for development. README owns setup/recovery details; #133 retains the remaining secret authority decisions.

The production transport is frozen below. Remaining security decisions include:

- credential-at-rest strategy before long-lived third-party credentials are stored
- master-key/secret recovery behavior

No external telemetry/analytics is enabled by default.

## Production serving (#131)

`postcardscene-web` runs Waitress 3.x through its Python API in one foreground
process. It reads the shared trusted `POSTCARDSCENE_CONFIG` file, validates before
listening, and starts no runtime workers. Debug/testing are forced off; startup
failure exits nonzero. Flask/Waitress diagnostics use fixed sanitized messages,
without exception text or request-access logging. The factory/CLI remains usable
without production serving settings. See [operations](../operations.md#production-control-plane-131)
for the exact configuration and installed paths.

Production requires nonempty finite-sequence Flask `TRUSTED_HOSTS`: string
entries without whitespace/control characters, schemes, paths or wildcard `*`.
Leading-dot subdomains remain supported; Flask owns final Host matching.
`SERVER_NAME` supplies neither a bind address nor an allowlist.

`WEB_TRANSPORT_MODE` defaults to `direct_http`; `WEB_BIND_HOST` defaults to IP
literal `127.0.0.1`; `WEB_BIND_PORT` defaults to integer 8080 (1024–65535, no bool).
`WEB_ALLOW_INSECURE_REMOTE_HTTP` is strictly boolean and defaults false.
Direct HTTP trusts no forwarded headers and forces non-Secure cookies.
Non-loopback/wildcard binds require explicit insecure opt-in: credentials and
session traffic are plaintext, suitable only for a deliberately trusted private
LAN, never untrusted networks or Internet exposure.

`reverse_proxy_https` requires binding exactly `127.0.0.1`, trusting only that
peer with count 1 and only X-Forwarded-For/Proto/Host. Waitress clears untrusted
forwarding headers; Flask never applies ProxyFix. Application-visible non-HTTPS
requests are rejected with a fixed 400 response before authentication/CSRF.
Flask validates the rewritten external Host. Secure cookies are forced true;
both modes retain HttpOnly, SameSite=Lax and the 12-hour session limit.
The same-host TLS proxy is operator/install authority; no TLS/DNS/proxy automation
is added. [Waitress options](https://docs.pylonsproject.org/projects/waitress/en/latest/arguments.html)
define the server's native one-hop forwarding semantics.

Installed runtime UID remains `postcardscene`; web UID is `postcardscene-web`
with primary group `postcardscene`. Web receives no runtime DRM/render/video/audio/
CEC/input groups and no `/run/postcardscene-wayland` authority. Its primary GID
satisfies the existing panel SO_PEERCRED check: runtime-owned `/run/postcardscene`
is 0750 with shared group, and `panel-control.sock` is 0660 with that group.
Web can send the fixed panel protocol but cannot replace runtime directory files.

Private signing authority defaults to `/var/lib/postcardscene-web/session.key`:
web-owned parent 0700, key 0600, exactly 32 bytes, regular file, no symlink or extra
hard link. RuntimeHost never reads it. Shared SQLite state stays under
`/var/lib/postcardscene` with group `postcardscene`. #26 provisions the identities,
secret via existing auth CLI as web UID, and exact DB/WAL/SHM modes/umask; #125/#26
must prove two-UID database operation without web graphics authority. Application
startup performs no recursive chmod/chown repair. #27 owns sensitive backup/restore.
#125 packages the [independent web unit](../operations.md#installed-web-service-125)
with no capabilities or device/Wayland access, read-only signing authority, and
writes limited to shared state plus private temporary buffering. Network and
fixed panel-client access remain available. #26 installs/provisions it; #29/#134
retain the final privilege audit.

## Privilege boundaries

The normal Flask service must not run with broad root privileges.

Potential privileged/device operations include:

- display/GPU/CEC/DDC device access
- service/install operations
- future controlled mount changes

Prefer normal Linux group/device permissions where possible. If elevation is necessary, use a narrowly scoped helper/service exposing only exact allowlisted operations and validated arguments.

Subprocesses should be invoked with argument vectors rather than untrusted shell construction. Avoid `shell=True` for application-controlled external commands.
