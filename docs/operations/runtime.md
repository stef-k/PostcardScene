# Runtime and services

Use this guide to operate the installed runtime and web services, configure the
production control plane, inspect owned storage, and manage playback and schedules.

Return to [Appliance operations](../operations.md). Provisioning and recovery are in
[Installation and diagnostics](installation.md); graphics, rendering and panel
contracts are in [Display, rendering and panel control](display.md).

## Installed runtime service (#122)

The package asset `postcardscene/runtime/systemd/postcardscene-runtime.service`
is the canonical RuntimeHost unit. #26 supplies the managed environment at
`/opt/postcardscene/venv`, installs/enables the unit for `multi-user.target`, and
provisions users, groups, directories and protected configuration. These assets
perform no installation or host mutation themselves. The independent web unit is described below.

The unit executes `/opt/postcardscene/venv/bin/postcardscene-runtime` directly as
`postcardscene:postcardscene`, with `Type=exec`. Its only application environment
setting is `POSTCARDSCENE_CONFIG=/etc/postcardscene/config.py`; both installed
application services use that trusted operator Python file. Never populate it
from ordinary web input. A specified missing/unreadable file fails startup.

Supervision is `Restart=on-failure`, `RestartSec=5`,
`StartLimitIntervalSec=60`, `StartLimitBurst=5`, `TimeoutStartSec=15`, and
`TimeoutStopSec=30`. `KillMode=control-group` and `SendSIGKILL=yes` bound remaining
process cleanup after cooperative SIGTERM. Thirty seconds accommodates sequential
component joins; the graphics service's five-second stop limit is insufficient
for the runtime. Clean SIGTERM exits zero without crash restart; fatal startup,
worker or cleanup failure exits nonzero. Repeated failures eventually hit the
start limit and require operator diagnosis. Boot activation requires #26 enablement.

`After=postcardscene-graphics.service` and `Wants=postcardscene-graphics.service`
order startup without coupling lifetimes. There is no `Requires`, `BindsTo` or
`PartOf`: graphics absence/recovery must leave catalog/background work alive.
Graphics clients check readiness before use. Renderers remain runtime-owned and
are never placed in labwc autostart. `Type=exec` confirms process execution,
not completion of application startup checks.

| Installed authority | Responsibility |
| --- | --- |
| `/etc/postcardscene/config.py` | Shared trusted operator configuration; #26 provisions/protects the file and parent. |
| `/var/lib/postcardscene` | Shared durable SQLite state, group `postcardscene`; never automatically cleaned. |
| `/var/lib/postcardscene-web` | Private durable web signing authority; web-owned 0700, key 0600. |
| `/var/cache/postcardscene` | Replaceable/regenerable application caches; later children own bounded use/cleanup. |
| `/run/postcardscene` | Transient state including panel-control socket; systemd/installer ownership, never durable or backed up. |
| `/run/postcardscene-wayland` | Independent graphics-service runtime directory. |
| Logs | Journald only in V0; no duplicate application `/var/log/postcardscene` tree. |

Application startup never recursively creates, changes ownership of, or deletes
these roots. Missing/foreign/unsafe authority fails or degrades at its owning
capability. #26 owns provisioning; #27 owns backup destinations and recovery.

The runtime CLI uses stdlib logging to stderr, captured by journald along with
stdout. Fixed messages report startup beginning, entry into normal service,
shutdown beginning/completion and fatal failure. Default diagnostics omit raw
exceptions/tracebacks, media paths, URLs, device selectors, environment values,
secrets and subprocess output. No UI status parses journal text. Inspect with
`journalctl -u postcardscene-runtime.service`; distro/systemd journal limits own
persistence/rotation. The application does not mutate `journald.conf`; #26/#28
retain host journal usage documentation/checks.

Restart constructs a fresh RuntimeHost and discards transient playback state.
Startup checks existing database compatibility/integrity without auto-migration
or repair. Unit/text/process tests establish software behavior only; actual Pi
boot, abrupt power-loss recovery and complete unattended evidence remain #127.

## Installed web service (#125)

The packaged `postcardscene/web/systemd/postcardscene-web.service` executes exactly
`/opt/postcardscene/venv/bin/postcardscene-web` as `postcardscene-web:postcardscene`.
Its only application environment setting is
`POSTCARDSCENE_CONFIG=/etc/postcardscene/config.py`. The [production serving
contract](#production-control-plane-131) owns all bind, Host, proxy and cookie
settings in that trusted file; the unit duplicates none of them.

The unit uses `Type=exec`, `Restart=on-failure`, `RestartSec=5`,
`StartLimitIntervalSec=60`, `StartLimitBurst=5`, `TimeoutStartSec=15`,
`TimeoutStopSec=30`, `KillMode=control-group`, `SendSIGKILL=yes` and
`WantedBy=multi-user.target`. Execution is not HTTP readiness. Invalid startup
exits nonzero and is eligible for restart/rate limiting. Default SIGTERM terminates
Waitress's foreground process, including its threads; it does not promise request
draining or emit the normal-return “stopped” message. Systemd treats SIGTERM as a
clean termination for this service type; explicit stop does not trigger restart.
After the stop timeout systemd kills remaining cgroup processes.

There is no runtime/graphics ordering or lifetime dependency in the web unit and
no reverse web dependency in those units. Stopping, restarting or losing one
service cannot propagate a systemd stop to the others. Control-plane availability
still depends on its own healthy configuration, signing authority and database.
Startup neither migrates nor repairs the database or filesystem permissions.

Hardening uses `NoNewPrivileges=yes`, empty bounding/ambient capability sets,
`PrivateDevices=yes` and `DevicePolicy=closed`. `ProtectSystem=strict` leaves only
`/var/lib/postcardscene` writable in the persistent filesystem. The private
`/var/lib/postcardscene-web/session.key` remains readable, but read-only inside the
service; initialize/recover it separately while web is stopped. `ProtectHome=yes`
hides home directories, and `InaccessiblePaths=-/run/postcardscene-wayland` masks
Wayland authority when present at startup. If graphics creates it later, its
runtime-owned 0700 mode still denies the separate web UID access. `PrivateTmp=yes` provides disposable
private temporary space needed for Waitress request/response buffering. It is not
a durable cache or an application log tree.

`RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` preserves the panel client and
both configured IP transport modes; no private network namespace is introduced.
The runtime-owned 0750 `/run/postcardscene` remains visible and read-only: connecting
to its 0660 socket does not require directory writes. The shared primary GID
satisfies the existing SO_PEERCRED check for the fixed `status|test_on|test_off`
protocol. Web gets no device supplementary groups or hardware command authority.

#26 provisions both identities, trusted config, shared DB/WAL/SHM permissions,
private web key, package/static assets and any proxy, then copies/enables this
versioned unit. Custom development database/key paths under home or outside these
installed roots are not supported by this unit. No directories, keys or schemas
are created by systemd startup hooks. #29/#134 retain the final privilege audit.

Stdout/stderr go only to journald: inspect with
`journalctl -u postcardscene-web.service`. Existing fixed startup/server diagnostics
omit request URLs, cookies, form fields and credentials. There is no access log
or duplicate `/var/log/postcardscene` tree. Diagnose repeated startup failures and
correct trusted configuration/provisioning before resetting the start limit.

Asset assertions and real foreground loopback tests cover both transport modes,
private-key loading, unchanged durable bytes, sanitized logs and bounded SIGTERM.
They model installed roots under the test UID. A separate local systemd user-service
smoke on 2026-09-09 used the built wheel and locked dependencies, the unit's
hardening/supervision properties, `PrivateUsers=yes`, and disposable bind-mounted
installed roots. Both transport modes served login/static assets, loaded the private
key, wrote SQLite, connected to the read-only runtime directory's panel socket,
and stopped through systemd. Home/device access was denied and the key was
read-only. The smoke used the test UID and invoked the same Python entry function
through the mapped environment; it does not prove installed two-UID DB/WAL/SHM
permissions or real runtime/graphics stop propagation. Those installed-host checks
remain required with #26 provisioning; physical Raspberry Pi/unattended evidence
remains #127. No physical-support claim follows from these software tests.

## Production control plane (#131)

V0 uses only Waitress 3.x, a pure-Python single-process threaded WSGI server,
through the packaged foreground command:

```bash
POSTCARDSCENE_CONFIG=/etc/postcardscene/config.py /opt/postcardscene/venv/bin/postcardscene-web
```

This is the installed launch contract, not an installation command. #125 owns
`postcardscene-web.service`; #26 provisions the environment, users and directories.
No runtime/background worker starts with web. The development Flask server is
never a production substitute. Invalid configuration/startup exits nonzero with
fixed stderr diagnostics; Flask/Waitress diagnostics omit raw values/tracebacks.
No request-access log is added.

The same trusted operator Python file used by RuntimeHost contains serving fields;
none belong in SQLite or administrator forms. A minimal loopback configuration is:

```python
TRUSTED_HOSTS = ["localhost", "127.0.0.1"]
# Defaults:
WEB_TRANSPORT_MODE = "direct_http"
WEB_BIND_HOST = "127.0.0.1"
WEB_BIND_PORT = 8080
WEB_ALLOW_INSECURE_REMOTE_HTTP = False
```

`TRUSTED_HOSTS` is mandatory and nonempty in production. Entries must be strings
without whitespace/control characters, URL schemes/paths or wildcard `*`.
Flask's leading-dot subdomain syntax is supported; Flask performs Host matching.
`SERVER_NAME` is neither a bind selector nor a Host allowlist. Bind hosts must be
IP literals; ports must be integers 1024–65535, with booleans rejected. The insecure
opt-in is strictly boolean. Direct HTTP ignores forwarded authority and forces
`SESSION_COOKIE_SECURE=False` even if configuration asks otherwise.

For deliberate private-LAN plaintext exposure, choose a non-loopback IP (or
wildcard IP `0.0.0.0`/`::`), set the appropriate `TRUSTED_HOSTS`, and explicitly
set `WEB_ALLOW_INSECURE_REMOTE_HTTP=True`. **Passwords and session traffic travel
in plaintext. This is unsuitable for untrusted networks or Internet exposure.**

Secure network-facing deployment uses a same-host HTTPS reverse proxy:

```python
WEB_TRANSPORT_MODE = "reverse_proxy_https"
WEB_BIND_HOST = "127.0.0.1"
WEB_BIND_PORT = 8080
TRUSTED_HOSTS = ["display.example.com"]  # replace with your external Host
```

Waitress accepts forwarding authority only from peer `127.0.0.1`, count exactly
1, trusting only X-Forwarded-For, X-Forwarded-Proto and X-Forwarded-Host. The proxy
must supply the actual client, external HTTPS scheme and external Host, replacing
untrusted inbound values. Other forwarding headers are cleared. No ProxyFix is
used. Requests without application-visible HTTPS receive 400; Flask validates the
external Host after Waitress rewrites it. Secure cookies are forced true.
Both modes retain HttpOnly, SameSite=Lax and the 12-hour session limit.
#26/#28 own proxy prerequisites/examples; this command installs no proxy, TLS
certificate, firewall rule or DNS configuration.

The installed web UID is `postcardscene-web`, primary GID `postcardscene`.
Runtime retains UID `postcardscene`. Web receives no runtime graphics/device groups
(DRM/render/video/audio/CEC/input) or access to `/run/postcardscene-wayland`.
Runtime-owned `/run/postcardscene` uses group `postcardscene`, mode 0750;
`panel-control.sock` uses that group, mode 0660. The web primary GID satisfies
SO_PEERCRED for only the fixed panel protocol, without directory replacement rights.

Shared SQLite remains `/var/lib/postcardscene/postcardscene.sqlite3`, with shared
application group access. #26 owns exact directory/file modes and umask, and
#125/#26 must prove web/runtime two-UID WAL/SHM writes. Neither process recursively
repairs ownership or permissions. Private signing authority is separately
`/var/lib/postcardscene-web/session.key`: web-owned directory 0700 and regular key
0600, exactly 32 bytes, no symlink/extra hard link. #26 provisions/initializes it
with existing auth CLI as the web identity; RuntimeHost never reads it. Existing
installations using an explicit `SESSION_SECRET_PATH` keep that override; no key
is automatically moved or regenerated. #27 owns sensitive backup/restore of this
secret and administrator DB state. These are software contracts, not evidence of
installed two-UID operation or physical Raspberry Pi/network validation.

## Owned storage snapshot (#123)

Authenticated **Overview** shows one **Owned storage** row for durable state
(`/var/lib/postcardscene`), replaceable cache (`/var/cache/postcardscene`) and
transient runtime storage (`/run/postcardscene`). Durable state appears first;
each class shows its current state and, when available, rounded free GiB.
A missing/inaccessible root is unavailable, including on an unprovisioned checkout.
Custom database locations are not discovered by this row; database integrity
remains a separate check.

Critical means free space **below 256 MiB or below 2%**. Otherwise warning means
**below 1 GiB or below 5%**; otherwise healthy. MiB is 1024 * 1024 bytes and GiB is
1024 * 1024 * 1024 bytes. Comparisons are strict: equality does not trigger that
dimension, but the other dimension can still degrade health. Critical takes
precedence, then warning, unavailable and healthy. Details retain all unavailable
checks even when another resource produces a worse overall state.

Free space uses filesystem `f_bavail`, excluding blocks reserved for root, with
fragment-size byte conversion (block size only when fragment size is zero).
Invalid statistics become unavailable. Refresh Overview for a new current host
snapshot; this is advisory, without automatic cleanup or background monitoring.
For degradation, check the named installed root's host capacity and accessibility.
No recursive sizing, mount discovery, deletion or database repair occurs. External
media/NAS are unowned, backups belong to #27, and journal usage/retention remains
host/systemd authority. #26 may reuse the seam for doctor and installation checks;
#126 retains cache cleanup policy.

## Playback worker capability (#89)

The independently constructible playback worker is tested with fake presenters.
The packaged runtime performs catalog work and optional #113 panel control; #93 will wire playback and
real image/video/web presenters after #58. There are no new operator launch settings,
Flask playback controls, display-power commands or hardware support claims here.

Integration starts one `PlaybackWorker` once and observes its shared RuntimeHost
stop event. Always call `join()` on host cancellation, including failure signalled
by the worker: it wakes long dwell waits and cancels in-flight work. Cleanup has a
five-second join bound; a fixed `failure` or join error requires nonzero runtime
exit so systemd can reap remaining process authority. Never restart another content
owner over uncertain cleanup. Presenter clear/stop must provide bounded retirement
and confirmed silence, including when the loading operation was cancelled.

The local Python command seam accepts `next`, `previous`, `pause`, `resume`,
`toggle_pause`, `seek_relative`, `mute`, `unmute`, `set_volume`, and
`set_output_suppressed`. Commands return Futures with fixed outcomes, never backend
exception details. Seek is limited to finite ±3600-second deltas and volume to
integer 0–100; actual availability and seek bounds remain presenter responsibilities.
A full 32-command mailbox returns busy. `request_shutdown()` bypasses the mailbox.
Do not wait for command results on the playback thread itself.

Pause holds remaining dwell and automatic progression; manual navigation retains
Pause, including video paused from its first authoritative state. Web pages remain
live while logically paused. Suppression retires output/audio and holds progression
without changing user Pause or claiming panel standby. Release revalidates the
current step; a video may restart from its beginning. Scheduling and panel protection
remain later owners of this mechanism.

Eight ordinary failed attempts produce safe degraded idle and a five-second backoff.
Intentional Idle is normal; disabled/no eligible configuration is degraded and
reevaluated boundedly. Public snapshots expose composition IDs and safe progress,
never source paths or URLs. They describe the last worker update rather than a
continuously refreshed clock. Restart discards Pause, current step, dwell and history.

## Weekly schedule and temporary overrides

Open **Schedule** after logging in. The page shows the saved configuration's
current Active/Sleep decision at page load, evaluated in the application timezone.
Change that single IANA timezone in **Settings**; Schedule has no separate zone.
The decision describes configured intent, not measured playback or panel power.

- **Disabled** means normally active all the time, preserving any saved windows.
- **Enabled** means active inside any matching weekly window and asleep outside.
- **Enabled with no windows** intentionally means sleep all the time.

Use **Add window** and **Remove window** to edit a draft of up to 64 weekday rows.
Multiple spans per day and overlaps are allowed; any matching span means active.
**Save schedule** replaces enablement and the complete window list together.
Invalid rows leave the saved configuration intact. **Discard draft** reloads the
saved state. Draft changes do not affect the displayed current decision.

Enter local times as `HH:MM`, from `00:00` through `23:59`. An **end of `00:00`**
means end of that day (`24:00`), so `00:00` to `00:00` is a full local day.
A window includes its start minute and excludes its end. Overnight activity needs
two explicit rows: for example Monday `22:00` to `00:00`, then Tuesday `00:00`
to `06:00`. A single `22:00` to `06:00` row is rejected. Sunday overnight activity
continues in a Monday row. Windows are never automatically merged or split.

Daylight saving follows the local wall-clock minutes that actually occur:
spring-forward skipped minutes never match; repeated fall-back minutes obey the
same window in both occurrences. Timezone changes and clock corrections cause a
fresh current-state evaluation, without replaying missed transitions.

The separate **Temporary override** form offers **Keep active** or **Sleep** for
an integer **1–10080 minutes** (at most seven days). It replaces any prior override,
persists across web/runtime restarts, and leaves weekly windows and enablement
unchanged. Expiry is stored as an absolute UTC instant and displayed in the
application timezone, including its UTC offset. Expired overrides no longer affect
the decision. **Resume schedule now** clears the override and returns to the saved
baseline. These actions do not save an unsaved weekly draft. There is no indefinite
override; Keep active never disables panel/static-content protection.

Flask only saves durable configuration. The schedule worker reevaluates within
15 seconds once connected, including after restarts and clock/timezone changes.
The worker capability exists, but its live playback/panel target and RuntimeHost
wiring remain #104; physical power and panel protection remain #10. Saving a
schedule or override does not yet control or prove panel standby/wake, playback,
or audio suppression. The page sends no runtime commands or hardware probes.

