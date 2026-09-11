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
See [operations](../operations/runtime.md#weekly-schedule-and-temporary-overrides).

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
IPC remains #29 authority. See [Display guidance](../operations/display.md#display-settings-status-and-tests).
See [wire protocol and operations](../operations/display.md#live-panel-runtime-and-local-control-113).

## Authentication, network exposure, and secrets

Initial authentication should be simple and appropriate to a self-hosted appliance.

Expected initial authority:

- administrator
- optional viewer/operator only when a concrete need appears

Issue #15 uses a single SQLAlchemy administrator (explicit `0002_administrator` migration), Werkzeug scrypt hashes, Flask-Login strong session protection and Flask-WTF CSRF. Logout/reset rotate its revocable login identity; all sessions are revoked.

Host CLI commands provision a protected persistent signing file and bootstrap/reset the administrator without database editing. Cookies are HttpOnly/SameSite=Lax, age-bounded to 12 hours; Secure is transport-derived in production and configurable for development. [Managed recovery](../operations/runtime.md#local-administrator-recovery-133) owns the installed procedure; README covers development setup.

### V0 secret authority and credential persistence (#133)

V0 does not implement or permit persistence of long-lived third-party provider
credentials: no provider passwords, API keys, bearer tokens, client secrets or
similar credentials in SQLite, trusted `/etc/postcardscene/config.py`, URLs,
Source/Widget JSON or any other persistence surface. Current semantic configuration
validators accept only their owned fields; trusted executable host configuration
and free-text URLs are not a secret detector. Operators must not embed credentials
in URL paths, queries or fragments either. No feature may use these surfaces as a
credential store. There is no encrypted credential table, vault, keyring, KMS or
master-key hierarchy in V0.

The first credentialed V1/V2 integration must introduce a separately reviewed
credential-at-rest design under #29 security authority **before persistence**.
It must define authenticated/encrypted storage, key authority, backup/recovery and
re-entry for that concrete provider. The session signing key is not an encryption
master key.

The protected 32-byte signing file is the sole signing/verification authority.
Configured `SECRET_KEY` and `SECRET_KEY_FALLBACKS` are non-authoritative. Missing
keys fail normal requests closed; unsafe or corrupt keys fail startup. Ordinary
startup never creates or replaces lost authority. Key loss/corruption is not
password recovery: it requires explicit installation/recovery action. Initialization
never replaces existing authority. Key bytes belong only in the protected file,
process memory and the sensitive #27 archive, never SQLite, trusted config,
command lines, logs/status/doctor output or release artifacts.

Administrator passwords remain scrypt hash-only; the local host-authorized CLI
prompts without echo and offers no plaintext persistence or web recovery bypass.
Reset changes only the password hash and persisted session identity, revoking all
previous browser sessions while preserving unrelated durable state. Errors and
diagnostics must not disclose passwords/hashes, session identities, key bytes,
session cookies or CSRF tokens. Browser cookies and CSRF form fields remain their
intended protocol surfaces, not diagnostics.

#27's unchanged sensitive recovery set classifies administrator password hash and
session identity as durable SQLite state, `session.key` as durable sensitive secret
authority needed for faithful signing continuity, and provider credentials as
**none in V0**. Restore recovers matching database/key authority consistently;
archive manifests contain only the existing identity/classification/checksums,
never secret values.

No external telemetry/analytics is enabled by default.

## Production serving (#131)

`postcardscene-web` runs Waitress 3.x through its Python API in one foreground
process. It reads the shared trusted `POSTCARDSCENE_CONFIG` file, validates before
listening, and starts no runtime workers. Debug/testing are forced off; startup
failure exits nonzero. Flask/Waitress diagnostics use fixed sanitized messages,
without exception text or request-access logging. The factory/CLI remains usable
without production serving settings. See [operations](../operations/runtime.md#production-control-plane-131)
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
#125 packages the [independent web unit](../operations/runtime.md#installed-web-service-125)
with no capabilities or device/Wayland access, read-only signing authority, and
writes limited to shared state plus private temporary buffering. Network and
fixed panel-client access remain available. #26 installs/provisions it; #29/#134
retain the final privilege audit.

## Control-origin abuse protection (#132)

The single-process Waitress service owns one memory-only login limiter keyed only
by `request.remote_addr`, after #131's server boundary. Five failed submissions
per IP fit in a rolling 300-second monotonic window; the fifth returns ordinary
401 failure, and later POSTs return 429 with whole-second `Retry-After` (1–300)
until the oldest failure expires. Success clears that IP's history. Invalid form
submissions count as failures; CSRF rejection remains 400 without password
verification. Already-limited POSTs are rejected before form/CSRF processing.
All login failures and throttles use the same fixed message and blank credential
fields. Known and unknown usernames both verify the single administrator hash.

Short locked memory operations reserve capacity before verification; no lock is
held across password hashing or database I/O, and no sleep/delay is inserted.
In-flight attempts reserve failure slots, with a one-second retry when only
pending attempts exhaust capacity. At most 256 IP entries are retained. Expired
idle entries are removed first, then the least recently active entry without an
in-flight attempt is evicted (ordered access/completion breaks ties). If all 256
entries are in flight, new IPs receive a one-second retry. Restart clears state;
shared NAT clients share a budget and IP churn can evict histories. This is bounded
appliance abuse resistance, not distributed or Internet-scale protection.

One response hook covers control/login/static/error responses with `nosniff`,
`Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, and Permissions-Policy
camera/microphone/geolocation/payment denials. CSP uses `default-src 'self'`,
self-only scripts/styles/fonts/connections, `object-src 'none'`, `base-uri 'none'`,
`frame-ancestors 'none'`, `form-action 'self'`, and no frames or workers.
Images allow only self and `data:` for vendored Bootstrap's embedded SVG controls.
The template inventory has no inline script/style/event handlers; first-party
control.js and Bootstrap/CSS need no nonce, hash or unsafe-inline exception.
No remote asset origins are allowed. This policy covers the control origin only,
not projected web scenes. Non-static responses retain `Cache-Control: no-store`.
Flask emits no HSTS; the external same-host HTTPS proxy owns it where appropriate,
while supported direct HTTP and #15/#131 session/cookie semantics remain intact.

## Privilege boundaries

The normal Flask service must not run with broad root privileges.

Potential privileged/device operations include:

- display/GPU/CEC/DDC device access
- service/install operations
- future controlled mount changes

Prefer normal Linux group/device permissions where possible. If elevation is necessary, use a narrowly scoped helper/service exposing only exact allowlisted operations and validated arguments.

Subprocesses should be invoked with argument vectors rather than untrusted shell construction. Avoid `shell=True` for application-controlled external commands.

## V0 trust-boundary audit (#134)

This implementation audit starts from `b10808db4c669cf0597a70fb1ef6f7165aa95d23`
and consumes #22/#64/#83/#111/#124/#125/#131–#133. It covers shipped capabilities,
including those awaiting runtime/physical integration; it does not certify a
release candidate, dependencies, artifacts or backups (#135). Outcomes are
`clear`, `hardened here`, `blocking issue required`, and `physical evidence`.
Test paths below are relative to `tests/`; they identify regression evidence,
not physical device proof.

| Boundary / ingress | Validator / owner | Authority granted | Process / UID boundary | Public diagnostic exposure | Regression evidence | Outcome |
| --- | --- | --- | --- | --- | --- | --- |
| Web Source URL and detached target | `web_selection` exact URL-only schema; `WebRenderer`; Chromium caller/final-URL validation | HTTP/HTTPS navigation, including intentional LAN/loopback targets; no userinfo, extra headers, cookie/token injection or arbitrary CDP | Untrusted runtime Chromium; no Flask browser work | URL hidden from target repr and renderer errors; full URL only in authenticated Source management | `test_web_selection.py`, `test_web_sources.py`, `test_web_renderer.py` | `clear` |
| Browser startup/profile/CDP | `ChromiumLaunchSpec`, context-marked `Profile`, `_cdp.PageControl` | One private profile/process/control session; deny downloads before content; reject caller file/script/data/extension/internal schemes; no broad file-access flags | Distinct trusted-image/untrusted-web roots and groups; runtime UID, separate from web signing authority; minimal child environment | No URL/profile/token/CDP port or raw browser output; private CDP logger does not propagate | `test_chromium.py` profile, launch isolation, malformed metadata/protocol and cleanup cases; download-order/environment assertions strengthened here | `clear` |
| Filesystem Source form/catalog identity | `PathPolicy`, `validate_source`, descriptor-relative traversal; `mounted_source` deepest NFS/NFS4/CIFS coverage | Read only beneath trusted roots; no descendant symlinks/special files; no mount command or root-policy edit in Flask | DB-only control health; runtime catalog and spawned mounted operations | Authenticated Source forms show configured paths; anonymous requests and health/error rows do not disclose them | `test_filesystem_source.py`, `test_local_directory.py`, `test_mounted_source.py`, `test_sources.py` | `clear` |
| Image/video bytes and catalog metadata | Shared no-follow `open_image_item` / `_open_media_item`; fresh Source snapshots; pinned-file mount check | Selected regular file only; image token routes or inherited video FD, never arbitrary media path API | Runtime-owned helpers; private image loopback server and mpv socketpair; no Flask media serving | Fixed failures, no original path/token in HTTP errors or access logs | `test_image_safe_open.py`, `test_image_delivery.py`, `test_video_file.py`; `test_catalog_metadata.py` decoder-open race and mounted-local rejection | `hardened here`: metadata now uses the existing pinned opener instead of reopening a checked pathname |
| Installed identities, DB and panel socket | Versioned units; `install_host.preserved_identities`; doctor identity checks; `PanelControl` / `panel_client` | Shared SQLite/WAL/SHM and fixed `status`, `test_on`, `test_off` protocol; no endpoint replacement | `postcardscene-web:postcardscene` versus runtime `postcardscene`; web has no supplementary device groups, capabilities or Wayland access; parent 0750, socket 0660 | Closed panel vocabulary only; no device selector/backend argument crosses the socket | `test_web_service.py`, `test_panel_control.py`, `test_display_ui.py`, `test_doctor.py`; existing `scripts/smoke_native_install.py` two-UID Linux lane | `clear` for software; device behavior is `physical evidence` |
| Config, status, errors and logs | Trusted config loader; typed runtime/status results; production `safe_diagnostics`; auth/recovery and doctor owners | Host configuration is executable host authority, never web-editable; public results grant no new authority | Signing key private to web; runtime configuration contains only its selected fields; no full parent environment to graphics children | No signing/password/hash/session/cookie/CSRF authority, raw CEC/DDC selectors, arbitrary environment, subprocess output or secret command lines in application diagnostics | `test_auth.py::test_known_diagnostics_and_errors_do_not_disclose_auth_values`, `test_web_server.py`, `test_runtime_service.py`, `test_configuration.py`, `test_resource_health.py`, `test_power_command.py`, `test_doctor.py` | `clear` |

### Complete application-owned subprocess inventory

The audit inspected Python `subprocess`, `multiprocessing`, `os.execve` and the
packaged systemd/labwc launch assets, including indirect users of shared runners.
Every external invocation is an argument vector; none uses `shell=True` or
constructs an untrusted shell program. Trusted host executable/package choices
are separate from Source/Widget/HTTP input. Python `-c` installation snippets are
fixed shipped code, not user command strings. Build/CI smoke scripts are tooling,
not V0 appliance entry points. Existing privileged lifecycle commands are listed
for completeness only; #134 adds no root command, sudo rule or setuid helper.

| Family / ingress | Validator / owner and granted authority | Process / UID | Output and cleanup boundary | Regression evidence | Outcome |
| --- | --- | --- | --- | --- | --- |
| Chromium (`chromium/_launch.py`) / host launch spec, validated target | Absolute package launcher, code-owned flags, private profile; URL travels only over private CDP | Runtime, separate pinned process group per context | stdout/stderr discarded; failed group retirement retains profile/process ownership and blocks replacement | `test_chromium.py`, `test_web_renderer.py` | `clear` |
| mpv playback (`video_player.py`) / pinned media FD and audio policy | `command_words`, read-only regular FD, `AudioPolicy`/`intended_device`; no config/scripts/autoload/references; fixed IPC operations | Runtime, pinned process group, inherited media and socketpair FDs only | No media filename in argv; stdout/stderr discarded; cleanup uncertainty prevents replacement | `test_video_player.py`, `test_video_file.py` | `clear` |
| mpv surface probe (`mpv_probe.py` through `_capability.HelperProcess`) / host launcher | Inert native-Wayland surface; no media/audio/scripts | Runtime pinned group | Capped private Wayland trace, fixed status; retained cleanup authority | `test_graphics_helpers.py`, `test_graphics_surfaces.py` | `clear` |
| GTK overlay (`overlay.py` through `HelperProcess`) / fixed helper and control state | Absolute system Python, packaged helper, bounded closed JSON/action protocol | Runtime pinned group, private inherited pipes | stderr discarded; protocol text never rendered as an error; failed retirement blocks launch | `test_graphics_helpers.py`, `test_graphics_input.py`, `test_controls.py` | `clear` |
| labwc and input emitter (`graphics/cli.py`, packaged XML/systemd) / host seat and local keys | Fixed `execve` vector, packaged config, allowlisted seat environment; fixed emitter path plus one typed action; no autostart/shutdown commands | Graphics/runtime UID; input socket inside owner-only 0700 Wayland root | Launcher/emitter failures fixed; labwc's own journal output is host diagnostics, not a public application feed; systemd owns cgroup retirement | `test_graphics_session.py`, `test_graphics_input.py`, `test_runtime_service.py` | `clear`; real seat/device behavior is `physical evidence` |
| wlr-randr (`output_probe.run_command`) / selected HDMI connector and advertised mode | Fixed `/usr/bin/wlr-randr`; bounded connector/mode parsing; output owner and shared mutation guard | Runtime; doctor may query only under appropriate runtime identity | Capped stdout, discarded stderr, safe connector/mode codes only; failed reaping is fatal shared cancellation before guard release | `test_graphics_output_command.py`, `test_graphics_output.py`, `test_output_monitor.py` | `clear` |
| CEC, DDC, signal (`power/_command.py`) / host selectors and panel intent | Fixed cec-ctl/ddcutil/wlopm; bounded `/dev/cecN`, DDC number/bus, selected HDMI-A-N; TV power, D6, or signal operations only | Runtime device groups; never Flask; signal shares output mutation guard | Capped capture/private signal trace; fixed result vocabulary; unreaped command latches owner against another command/backend | `test_power_command.py`, `test_power_backends.py`, `test_panel_coordinator.py` | `clear`; physical confirmation is `physical evidence` |
| Mounted scan/metadata, image-frame, mounted-video helpers (`mounted_source.py`, `image_delivery.py`, `video_file_worker.py`) / validated Source and selection | Fixed Python spawn targets, shared path authority; image helper token routes; video SCM_RIGHTS transfer | Runtime children, private Pipe/socketpair; image listener loopback only | Fixed IPC failures; bounded terminate/join/kill; incomplete traversal never authorizes deletion; unreapable read helper is an error, not successful cleanup | `test_mounted_source.py`, `test_catalog_metadata.py`, `test_image_delivery_lifecycle.py`, `test_video_file.py` | `hardened here` for metadata read authority; other launch contracts `clear` |
| Doctor child and systemctl query (`doctor/bounded.py`) / closed check identifiers | Fixed check functions/unit names; systemctl show and read-only graphics query; no dynamic config execution | Invoking host identity; no impersonation/device escalation | Child stdout/stderr suppressed; capped tool result, closed schema; check process group killed at deadline | `test_doctor.py` | `clear` |
| Backup worker and restore systemctl (`backup/__init__.py`, `restore_host.py`) / owned operation/path | Fixed isolated Python module and operation; fixed managed unit set; destination validation remains #27 | Existing web-UID CLI/oneshot; restore is explicit root host operation, never Flask | Worker stderr discarded, bounded result pipe; inherited mutation lock prevents duplicate mutators; restore failure remains fail-stopped | `test_backup.py`, `test_backup_scheduled.py`, `test_restore_host.py`, `test_restore_execution.py` | `clear` for launch boundary; archive/recovery audit excluded |
| Native preflight (`install_preflight.Host.command`) / fixed host/package/tool queries | Closed distro/tool/package selectors; systemd-detect-virt, dpkg/dpkg-query, apt-cache, systemctl, snap, Python/GI and tool-version probes | Existing host-admin preflight | Minimal environment, capped capture and fixed reports; group cleanup on exit/deadline | `test_install_preflight.py` | `clear` |
| Managed install/remove/update (`install_command.command`, `install_host`, `install_services`, `install_update*`) / authenticated shipped lifecycle code | Fixed apt-get/snap, user/group provisioning, systemd-tmpfiles/systemctl, isolated Python venv/pip/Flask/schema helpers; package/path/phase authority owned by #26 | Existing root CLI; explicit runtime/web child UID with supplementary groups cleared | Noninteractive output discarded or bounded; interactive admin password prompt never puts password in argv; fixed CLI errors; inherited update/recovery lock and fail-stopped phase ownership | `test_native_install.py`, `test_update_transaction.py`, `test_update_recovery.py`, installed-Linux lane | `clear` for launch boundary; artifact/update audit excluded |

### Findings and deliberate residual limits

The local finding is a check/open race in catalog image metadata: Pillow formerly
received a pathname after validation. A regression reproduced reading an outside
image when that name became a symlink. Metadata now passes the existing pinned
no-follow stream to Pillow, checks the opened mount for mounted Sources and
rechecks that descriptor's freshness. No new filesystem policy or decoder exists.
The audit found no substantial finding requiring a blocking issue.

LAN/loopback web targets are intentional browser network authority. Normal page
scripts and browser-origin rules still apply; V0 supplies neither a network
allowlist nor a server-side fetch/proxy/scanner. Do not configure credentials in
URLs or sign the kiosk into administration. One replaceable untrusted profile
shares ordinary site state across Sources; there is no per-Source isolation or
supported login/session provisioning.

Loopback CDP is private from the LAN and application API, **not Unix-UID-authenticated**.
Its random port and protected metadata do not constitute isolation from malicious
local host processes. Trusted/untrusted browser groups run under the same runtime
UID; OS/browser compromise, hostile host administrators and local process inspection
are outside this frozen boundary. Browser sandbox/TLS protections are not disabled;
the URL validator and profile separation are not a generic OS sandbox.

Host-controlled media roots/ancestors and Linux mount administration remain trusted.
The deepest mount and pinned-file checks reject local fallthrough, but do not
authenticate server/export identity at the same mountpoint or freeze in-place
file writes. Read-only helpers cannot be guaranteed reapable during kernel
uninterruptible sleep; they report incomplete/error and rely on outer host/service
recovery. Mutable renderer/panel/output ownership instead fails stopped as above.

Application diagnostics discard or sanitize child output. The directly exec'd
labwc service retains native protected journald diagnostics under #62; it receives
fixed launch/configuration authority, not configured URL/password arguments.
Do not expose that journal, private profiles or process command lines as a public
status feed. #66/#116/#127 retain real package, seat, device, HDMI and panel evidence;
software tests and the installed-Linux lane cannot close those gates. #135 retains
exact-candidate security/dependency closure.
