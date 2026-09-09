# Appliance operations

This document records implemented operating contracts, including #140 initial
managed installation. Publication and updates remain later #26 work; installed
software evidence is not a claim of a physically validated release.

## Release inputs (#138)

Released CPython 3.11–3.14 is the application compatibility boundary. Generic
Linux CI is Python evidence only; Raspberry Pi ARM64/graphics/device support
still requires the owning physical validation gates.

The versioned application input is `postcardscene-<version>-py3-none-any.whl` plus
`runtime-requirements.txt`, generated from the exact `uv.lock`. Installation must
use hashes and binary dependencies only, followed by the wheel with `--no-deps`;
a missing compatible dependency wheel fails rather than compiling on the target.
No configuration, signing key, database, cache/profile or backup belongs in these
release inputs.

The future GitHub Release `postcardscene-<version>-linux-native.tar.gz` will carry
those inputs, standalone `install.py` (#140), `install_preflight.py` (#139),
and `release-manifest.json` (#143), plus explicitly reviewed metadata/checksums.
Version comes only from
`pyproject.toml`; tag, wheel, installed/Overview and manifest identity must agree.
Publication and updates remain later work. See
[development checks](../README.md#release-input-development) and the
[release contract](architecture/installation-recovery-and-operations.md#deterministic-release-inputs-138).

## Read-only managed-host preflight (#139)

Run `python3 -B install_preflight.py` from the source/native support files, or add
`--json` for the frozen result schema (`ok`, fixed `reasons`, nullable `plan`).
This standalone stdlib module needs no installed PostcardScene or root execution.
#140 `install.py` consumes `preflight()` directly; #143 ships the module
beside it. Exit 0 means the clean host has a representable provisioning plan;
exit 1 means inspection failed and there is no plan. It does not mean packages
are already installed, conflicts resolved, or physical playback validated.

| Managed target (Raspberry Pi 4/5-class ARM64) | Package authority |
| --- | --- |
| Ubuntu Server 24.04 LTS / Noble | Ubuntu archive native tools; Canonical Chromium snap, `latest/stable`, `/snap/bin/chromium`. |
| Ubuntu Server 26.04 LTS / Resolute | Same authority and native runtime contracts. |
| Raspberry Pi OS 64-bit / Debian 13 Trixie, preferably Lite | Debian/Raspberry Pi archive native tools and `chromium`, `/usr/bin/chromium`. |

Point releases retain these release identities. Exact `ubuntu` with its matching
release/codename, or `debian`/`raspbian` 13 with `trixie`, are recognized; `ID_LIKE`
does not authorize derivatives. Machine ARM64, 64-bit bootstrap userland and dpkg
`arm64` must agree. Containers, WSL and hosts without systemd PID 1 are rejected.
This is the managed target matrix, not proof of Raspberry Pi model or HDMI support.

The fixed apt set includes `labwc`, `wlr-randr`, `wlopm`, `mpv`, `ddcutil`,
`v4l-utils` (owns `/usr/bin/cec-ctl`), `python3`, `python3-venv`, `systemd`,
`systemd-sysv`, `libpam-systemd` and `libseat1`, plus Ubuntu `snapd` or Debian
`chromium`. Other native tool paths are `/usr/bin/<tool>`. Default seat authority
is active logind plus PAM; `--seat seatd` explicitly adds `seatd` and a socket
access provisioning action, without automatic fallback. The packaged #62 PAM/VT
and seatd templates remain authority for later installation.

`/usr/bin/python3` must already provide distro-owned CPython 3.11–3.14 and
importable venv/ensurepip with an available pip version. Missing bootstrap support
fails `python_bootstrap_required`; an operator must supply the distro prerequisite
before retrying. Preflight never creates a venv or installs into system Python.
Missing native tools return `install_required` only when cached apt candidates
have supported origins. No apt update/download is performed. Candidate and
installed versions must be represented by the official Ubuntu ports/archive/
security or Debian deb/security/Raspberry Pi archive URLs; custom mirrors and
locally supplied package versions require explicit operator resolution.

Installed executable authority is checked against dpkg ownership/integrity and
root-controlled paths before bounded version probes. Modified packages, missing
installed executables and malformed versions fail closed. Chromium uses package
or local snap metadata, never a browser launch. Ubuntu's `chromium-browser` is
only a transition to the snap; its wrapper alone cannot satisfy Chromium.
Installed labwc retains #62's 0.7.1 floor; no new media/panel version minimum is
invented. Package presence cannot prove seat, codec, audio, CEC or DDC behavior.

The installed roots in the table below, plus `/opt/postcardscene`, are
inspected only through existing ancestors/targets. Symlinks, foreign/writable
ancestors and unrecognized existing roots fail closed. Supported local persistent
filesystems are ext2/3/4, btrfs, xfs, f2fs and zfs; `/run` may also use tmpfs.
Unknown/network mounts are rejected before inspecting their installation paths.
Existing PostcardScene accounts or units are unrecognized until #140/#142 define
managed recognition; even empty roots are never adopted. Loaded tty1 getty or
display-manager units produce explicit `reserve_tty1`/`resolve_display_manager`
actions, not implicit permission to stop or disable them.

This snapshot makes no package/user/config/service/database mutation, recursive
root scan or repair. Commands use fixed argv, a clean environment, five-second
limits and 256 KiB output caps; failures omit raw output, identities and paths.
The later installer must recheck current authority before mutation and explicitly
handle every action. It must not treat serialized JSON as trusted installer input.
Fixture tests and local Ubuntu x86/WSL rejection/package-query observations are
software evidence only. ARM64 boot, real seat/device access and physical display
validation remain with #66/#116/#127 and the release-candidate gates.

Package evidence checked for #139: [Ubuntu release families](https://packages.ubuntu.com/),
[Noble labwc 0.7.1/arm64](https://packages.ubuntu.com/noble/labwc),
[Resolute wlopm](https://packages.ubuntu.com/resolute/wlopm),
[Ubuntu Chromium transition](https://packages.ubuntu.com/en/chromium-browser),
[Canonical Chromium snap](https://snapcraft.io/chromium),
[Trixie Chromium](https://packages.debian.org/trixie/chromium) and
[labwc](https://packages.debian.org/trixie/labwc),
[v4l-utils arm64 file list](https://packages.debian.org/trixie/arm64/v4l-utils/filelist),
and [Raspberry Pi OS Trixie images](https://www.raspberrypi.com/software/operating-systems/).
These are package/provisioning evidence, not hard-coded current version promises.

## Managed initial installation (#140)

From one trusted, reviewed extracted input set, run `sudo python3 -B install.py
install` in an interactive terminal. Keep exactly one application wheel beside
`install.py`, `install_preflight.py` and `runtime-requirements.txt`; no checkout or
preinstalled application is needed. The installer validates wheel identity,
Python/pure-wheel metadata, entry points, required assets and wheel RECORD hashes.
It checks the reviewed preflight and requirements bytes against installer input
pins **before importing support or mutating the host**. The installer itself is
trusted executable bootstrap authority. These checks do not authenticate a
published release: #143 owns its final manifest schema, member hashes, extraction
and publication checks. No final release-manifest format is defined here.

The only initial path uses #139's default logind/PAM plan. Standalone preflight's
explicit seatd inspection remains available; this initial command does not select
seatd. Failed preflight or a non-root/non-interactive invocation changes nothing.
Existing accounts, roots, config or units fail closed, including repeat invocations;
installation never adopts legacy deployments, resets an administrator or replaces
a signing key. Preserved-state reinstall and updates remain #142/#144.

The mutation sequence is:

1. Install the closed apt prerequisites (600-second update/900-second install
   bounds) and, on Ubuntu, Canonical Chromium stable snap (600 seconds). Recheck
   the clean-host preflight and require every planned tool to be installed before
   creating application roots. Package failures retain distro package-manager
   evidence and never trigger package rollback or application-state mutation.
2. Create a root-controlled `/opt/postcardscene/releases/<wheel-version>/venv`,
   using the supported distro Python. Install pinned requirements with required
   hashes and binary-only policy, then the app wheel with `--no-deps`. Check pip
   consistency, installed version, imports, entry points and packaged files before
   bootstrap or activation. No distro-Python pip installation or source build runs.
3. Create locked, nologin `postcardscene` and `postcardscene-web` users with shared
   primary group `postcardscene`. Runtime supplementary groups are selected only
   from actual root-owned group-readable/writable DRM, sound, CEC and I2C character
   devices and their allowlisted `render`/`video`/`audio`/`i2c` groups. Logind owns
   session device access. Web receives no supplementary/device/seat groups.
4. Provision the authorities below, copy wheel-owned systemd/PAM assets, and keep
   labwc configuration in the versioned wheel where its launcher already reads it.
   Install web/runtime `UMask=0007` drop-ins and a tmpfiles entry for reproducible
   `/run/postcardscene` creation. Record prior getty/display-manager states and
   local unit symlink targets in root-controlled
   `/opt/postcardscene/service-conflicts.json`, disable/stop loaded conflicts and
   mask tty1 getty. No desktop profile is rewritten.
5. Require inactive application services. Exclusively reserve the new empty
   database as the runtime UID with mode `0660`: SQLite's default initial `0644`
   cannot acquire group write through umask alone. Alembic owns all DB content.
   As the web UID with umask `0007`, run
   packaged `auth init-secret`, `db upgrade`, `db check` and `auth create-admin`.
   Username/password prompts use the interactive CLI; passwords never enter argv,
   environment, config or an installer log. No default password is generated.
6. Atomically create `/opt/postcardscene/venv` pointing to the validated release
   venv. Only then daemon-reload, enable graphics/runtime/web for multi-user boot,
   start them in that order and check active state. Existing independent service
   lifetime policies remain intact. Active units do not prove display readiness.

| Authority | Installed owner/group and mode |
| --- | --- |
| `/opt/postcardscene` and releases | root-controlled, directories 0755; payload not service-writable |
| `/etc/postcardscene`, `config.py` | root:postcardscene 0750 / 0640 |
| `/var/lib/postcardscene` | postcardscene:postcardscene 2770; SQLite/WAL/SHM 0660 |
| `/var/lib/postcardscene-web`, `session.key` | postcardscene-web:postcardscene-web 0700; web-owned key 0600 |
| `/var/cache/postcardscene` | postcardscene:postcardscene 0700 |
| `/run/postcardscene` | postcardscene:postcardscene 0750; panel socket 0660 |
| `/run/postcardscene-wayland` | graphics unit-owned 0700, independent of shared IPC |

Initial config selects the local shared database, private signing-key path and
loopback trusted Hosts. #131's direct loopback HTTP defaults remain in effect;
remote HTTP is not enabled. No provider credentials are written. Trusted roots
must be real directories; existing files are never overwritten and unknown trees
are never recursively chowned/chmodded.

### Install failures and evidence

Failure exits nonzero and reports the failed phase. Before bootstrap begins,
cleanup removes only the newly staged release; created config/identities/roots,
package changes and conflict records remain for inspection. After bootstrap starts,
payload, config, database and key are preserved without downgrade or rollback.
Activation failures attempt to disable and stop the new application units; a
failed stop/disable is reported separately and requires operator attention before
recovery or reboot. Do not delete durable authority or retry this as an update.

For a bootstrap failure, keep services inactive and inspect the named release's
venv and `/etc/postcardscene/config.py`. A host administrator can open a shell as
`postcardscene-web`, set `umask 0007` and
`POSTCARDSCENE_CONFIG=/etc/postcardscene/config.py`, then use that staged venv's
`python -m flask --app postcardscene.web:create_app` with `auth init-secret`,
`db upgrade`, `db check` and, only if initial creation did not complete,
`auth create-admin`. These existing commands validate/reuse key/schema authority;
first verify the database is runtime-owned, group `postcardscene`, mode `0660`.
If reservation itself failed, investigate the conflict before any migration;
do not let recovery implicitly create a replacement database with SQLite defaults.
never use password reset as an install retry. Have the host administrator verify
all phases before creating the active symlink or enabling services. Earlier
partial provisioning requires deliberate host reconciliation; this command has
no automatic cleanup/adoption/resume engine. The saved conflict record identifies
which prior boot services were changed for later safe removal.

The privileged disposable Linux CI smoke stages the real wheel at the installed
paths, creates genuinely distinct UIDs, uses normal persistence transactions in
both directions with live WAL/SHM sidecars, and exercises the group-authorized
panel socket. It proves private-key, socket replacement, Wayland and representative
character-device DAC separation and starts canonical runtime/web units under their
installed identities. It deliberately bypasses ARM64/package detection on the CI
VM. Graphics boot, actual distro provisioning on ARM64, HDMI/GPU/audio and physical
Pi behavior remain unverified; #66/#116/#127 retain that evidence.

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

## Linux graphical session (#62)

Raspberry Pi 4/5-class ARM64 is the primary V0 hardware class. **Ubuntu Server LTS
ARM64 on Raspberry Pi and Raspberry Pi OS 64-bit are both primary targets** using
one native Wayland/labwc implementation. Windows is outside V0; the graphics
module is separate from platform-neutral application/runtime behavior.

| Distribution path | Package/provisioning boundary |
| --- | --- |
| Ubuntu Server 24.04 LTS ARM64 | `labwc` 0.7.1 is in Universe for arm64; retain that configuration compatibility floor. Use systemd-logind/libseat with a service-owned local VT session. |
| Ubuntu Server 26.04 LTS ARM64 | `labwc` 0.9.3 is in Universe for arm64; use the same application files and session contract. |
| Raspberry Pi OS 64-bit, current stable Trixie | Use its packaged `labwc`/wlroots/libseat, independently of Raspberry Pi Desktop helpers. Prefer the same logind session where supported; seatd is an explicit provisioning alternative. |

Package evidence: [Ubuntu package index](https://packages.ubuntu.com/search?keywords=labwc),
[Raspberry Pi OS documentation](https://www.raspberrypi.com/documentation/computers/os.html).
Package availability is not Pi/HDMI validation. #66 must record exact OS/kernel,
package versions and non-root seat/device smoke on both distro families before
physical support claims are made. Do not substitute a different renderer stack
when one distro needs provisioning corrections.

### Ownership and boot provisioning contract

The installed package contains `postcardscene/graphics/systemd/`, `pam.d/` and
`labwc/` assets. #26 installs the unit and PAM file under their standard `/etc`
locations and supplies the isolated Python environment referenced by the unit
(`/opt/postcardscene/venv/bin/python` is the template payload location). It creates
the dedicated, locked-password, non-root `postcardscene` user/group and enables
`postcardscene-graphics.service` for `multi-user.target`. This issue does not
install packages, create users, copy files into `/etc`, or enable host services.

The default system service owns tty1 through `PAMName=postcardscene-graphics`,
`pam_systemd`, `StandardInput=tty` and `TTYPath=/dev/tty1`. Logind registers the
local Wayland session and libseat acquires DRM/input device authority from it.
No shell login, display manager or general desktop is involved. Provisioning must
reserve and activate tty1 at boot, disable its getty/autologin and any competing
display manager, and ensure `libpam-systemd`/logind and the packaged libseat logind
backend are available. `Conflicts=getty@tty1.service` prevents concurrent ownership;
masking/disabling conflicting boot services belongs to #26. Do not change an
administrator's normal desktop profile. Verify the session is active on seat0
with `loginctl session-status` on the target hardware.

Where seatd is the appropriate distro-supported authority, install the packaged
`systemd/seatd.conf` as a service drop-in. It clears PAM session creation and sets
`LIBSEAT_BACKEND=seatd`; provision seatd and permission to its standard local
socket through the distro's seat-access group. It uses the same user, VT, labwc
files and Python capability. No application-level distro detection chooses a
backend. Inspect actual udev/group policy before granting render/video/audio
access needed by later clients; never solve device access by running Flask,
labwc, the runtime or renderers as root. Do not apply `PrivateDevices` or other
hardening that hides required devices/session authority without validating it.

Systemd creates `/run/postcardscene-wayland` owned by the graphical user, mode
`0700`, with `UMask=0077`. This local, replaceable directory is independent of
PAM's general `/run/user/<uid>` directory. The exec-only launcher validates it,
rejects leftover `wayland-*` entries, and supplies it as `XDG_RUNTIME_DIR`.
Labwc allocates its first socket, `wayland-0`, in that private directory. Systemd
owns directory removal; the application never unlinks a foreign socket to make
startup succeed. Keep the directory's ancestors under trusted host control.

### Environment, readiness and lifecycle

`postcardscene.graphics.WaylandSession` is the shared ordinary-Python seam.
`client_environment()` validates ownership/mode and returns only the explicit
Wayland/runtime-directory and toolkit variables. Later Chromium/mpv launchers
compose these with their own narrow environment and native-Wayland engine
arguments. Do not merge the parent's full environment: omit `DISPLAY` and
`WAYLAND_SOCKET`, and never import the administrator's desktop activation state.
This issue starts neither renderer nor the application RuntimeHost.

`inspect(compositor_pid=...)` returns a frozen `SessionStatus`: availability,
compositor state, runtime-directory validity and a fixed diagnostic reason.
The optional PID is the compositor service's `MainPID`; a different socket peer
is rejected. Without it, the probe still requires the current non-root user's
private directory and owned socket. Readiness requires a core Wayland sync reply
within 250 ms, not merely a socket file, accepting listener or cached ready flag.
The probe does not establish a connected output, visible frame, acceleration or
physical panel state. Public diagnostics contain only fixed fields/vocabulary,
never arbitrary environment values, paths, exceptions or secrets.

`Type=exec` tracks labwc directly: **systemd active means process activation, not
graphical readiness**. Consumers must inspect the capability before launching
clients. A failed exec or compositor crash exits nonzero; systemd retries after
five seconds, with five starts per minute before visible failed state. A live
but unresponsive compositor remains unavailable to callers; later runtime policy
owns any recovery decision. `RuntimeHost` remains application lifecycle authority;
there is no second application loop, restart supervisor or durable ready state.

Use these commands as the graphical user on a provisioned host:

```bash
/opt/postcardscene/venv/bin/python -m postcardscene.graphics.cli status
/opt/postcardscene/venv/bin/python -m postcardscene.graphics.cli wait --pid <labwc-main-pid>
```

`status` returns JSON and exits nonzero if unavailable. `wait` polls the same
capability for at most roughly ten seconds (including bounded probe overhead),
exits immediately for an exited/mismatched compositor or invalid directory, and
emits only safe diagnostics on failure. Get the PID from
`systemctl show --property=MainPID --value postcardscene-graphics.service`.
Keep raw compositor journald logs in host-authorized diagnostics. Do not equate a
successful `systemctl start` with the result of this readiness check.

Stopping the service sends SIGTERM to labwc; the five-second stop bound escalates
to SIGKILL if necessary. Repeated `systemctl stop` is safe. No shutdown hook starts
work. The PAM scope may own compositor children separately from the service
cgroup; this contract starts no lasting graphical child or renderer there.
Later renderers must belong to RuntimeHost's own bounded lifecycle, not labwc
`autostart`, and stop their processes when their graphical connection is lost.
A task stuck in uninterruptible kernel sleep cannot be guaranteed reaped by a
timeout; failed cleanup must remain visible rather than be called clean.

### Appliance presentation and validation limits

The dedicated `-C` configuration directory prevents fallback to personal/distro
configuration or autostart files. Explicit inert key/mouse bindings prevent
labwc 0.7.1 from restoring its terminal/menu defaults when lists are empty.
`XKB_DEFAULT_OPTIONS=srvrkeys:none` removes ordinary VT-switch keysyms. There are
no launch actions, menus, panel, notifications or wallpaper helper. Empty labwc/
wlroots scene composition clears to opaque black, including between renderer
surfaces. This is graphical fallback, not panel standby, boot-firmware blanking,
or a physical-access security boundary. #26/#66 must verify boot console/getty
policy; #10 owns power and panel protection.

XWayland is outside the application contract. Older labwc builds may initialize
lazy X sockets internally; `WLR_XWAYLAND=/usr/bin/false` refuses an attempted X
server launch, and application clients receive no X display. Both distro paths
must use native Wayland clients. #64 owns Chromium controls/isolation; #65 owns
mpv/surface/overlay proof. #63 output selection and hotplug are described below.

A controlled Ubuntu 24.04 x86-64/WSL smoke used distro labwc 0.7.1 and wlroots
0.17.1, extracted into a temporary directory, with headless/pixman backends and
the packaged appliance configuration. It answered the real sync probe, loaded
no default key/mouse bindings, exited zero on SIGTERM, and became unavailable
after exit. A 1280x720 capture confirmed a black background with the default
pointer; pointer visibility/overlay behavior remains #65. This is software/session
evidence only; headless backends are not
production launch options. Pi ARM64, unattended boot/logind/seatd authority,
HDMI/4K, input behavior and both-distro physical evidence remain with #66.


## HDMI output reconciliation (#63)

The graphical/runtime user calls
`postcardscene.graphics.output.reconcile_display(WaylandSession())` to inspect
and, when necessary, configure the one HDMI output. This is a mutating Python
operation, not a read-only doctor command. Its frozen `DisplayStatus` has
`public_diagnostics()` for safe serialization; the existing session CLI remains
session-only. #124 starts one output monitor in the configured runtime executable,
including when panel power is disabled; no additional feature switch is required.

#26 must provision `/usr/bin/wlr-randr` with working `--json` support and the
wlr-output-management protocol on labwc. The same executable path, session
capability and DRM sysfs policy apply on Ubuntu Server and Raspberry Pi OS.
An older tool lacking JSON support reports `tool_failed`/`malformed_output`;
there is no text-output or desktop-session fallback. Verify the installed tool
and compositor combination during #26/#66 provisioning.

V0 selects one connected HDMI-A connector. With multiple, the trusted Python
caller can pass `connector_override="HDMI-A-1"`; this is host configuration,
not a database/UI/Scene setting. A missing/disconnected override never selects a
different connector. Conflicting DRM card names or absent Wayland identity fail
closed. Non-HDMI outputs are ignored and no output is switched off by this policy.

The preference is advertised 4K60-class, then 1080p60-class, then safe preferred
or current <=60.2 Hz. Scale 1, position 0,0 and normal transform are enforced.
Unsupported high-refresh-only/ambiguous timings remain degraded. Successful
application requires fresh matching Wayland state. This does not prove physical
panel power, visible pixels, cable bandwidth or validated 4K operation.

A runtime-owned consumer can iterate `monitor_display(session, stop_event)`.
It reconciles immediately and waits interruptibly for one second between calls,
including after failures. Connect/disconnect/reconnect converge on later polls;
no-display is normal and skips tool execution. The owner must consume snapshots
promptly, share its shutdown Event and stop consuming when appropriate. Commands
are capped at two seconds and 256 KiB of stdout; cancellation is checked every
50 ms during command waits, with up to 250 ms for kill/reap. The session probe
retains its 250 ms bound. Sysfs enumeration is capped at 256 entries, status at
32 bytes per connector and EDID at the 128-byte base block. As with other local
kernel I/O, a driver stuck in uninterruptible sleep cannot be given a Python
wall-clock guarantee; #66 must exercise the actual hardware/driver path.

| State/reason | Meaning and operator action |
| --- | --- |
| `no_display` | No connected eligible HDMI connector; compositor remains alive. |
| `ready` | Selected output matches software mode/layout policy. |
| `ambiguous` | Multiple connected HDMI connectors; provide an exact host override. |
| `session_unavailable` | Check the #62 session capability and service/seat provisioning. |
| `drm_failed`, `connector_mismatch`, `invalid_connector_override` | Check connector presence, host override and DRM/Wayland identity; retry on a later poll. |
| `tool_missing`, `tool_failed`, `tool_timeout`, `oversized_output`, `malformed_output` | Check installed wlr-randr/JSON support and compositor responsiveness. Raw command output is never published. |
| `no_safe_mode`, `ambiguous_mode`, `apply_failed` | No proven selectable safe mode or fresh post-state disagrees; inspect the provisioned tool/display combination. |
| `cancelled` | Cooperative runtime shutdown interrupted a command. |

Diagnostics retain only manufacturer letters and hexadecimal product code from
a checksum-valid EDID base block; missing/malformed EDID is optional diagnostic
loss. No monitor serial, model/description text, raw EDID, environment or command
output is retained. Snapshots and this policy have no durable state to back up.

Unit/process tests prove software decisions and bounded tool behavior. Physical
Pi/HDMI/4K/hotplug claims remain #66. Panel standby/wake stays with #10; #124
suspends and serializes this reconciliation against intentional signal sleep.
Renderer lifecycle and shared surfaces remain #64/#65.

## Isolated Chromium control (#64)

The implemented controller lives in `postcardscene.graphics.chromium`. It is not
yet started by the runtime CLI: #58 owns image-adapter integration, #82 web
URL/session policy and #83 web rendering, #65 shared Chromium/mpv/overlay surfaces, and #8 playback.

#26 must supply `ChromiumLaunchSpec(command, profile_root, environment={})` from
trusted host configuration. `command` is a tuple whose first element is an
absolute launcher path; remaining words are package invocation arguments, never
browser options (prefix words beginning with `-` are rejected). A direct binary,
installer-owned wrapper, or package invocation uses the same controller and
code-owned flags. No `/usr/bin/chromium` assumption or distro detection exists.
Launchers must forward the appended argv, stay in the owned process group, and
exec or wait for Chromium; daemonizing/escaping the group is unsupported.

Provision two distinct, non-nested, initially empty local roots owned by the
dedicated non-root graphical/runtime user, mode `0700`, beneath trusted ancestors.
Do not select an administrator's browser profile or an SMB/NFS path. Construction
creates a context marker and a private `profile/` user-data directory; subsequent
starts validate them and lock the marker. Cross-context reuse, symlinks, foreign
ownership, loose permissions and adoption of nonempty unmarked roots fail closed.
No cookie/history/profile copy or recursive permission/deletion operation occurs.

Chromium's HOME/config/cache locations stay under its own root. The optional
non-secret overlay accepts only `PATH`, `LANG`, `LC_ALL`, `SNAP_NAME` and
`SNAP_INSTANCE_NAME`; it cannot replace the #62 Wayland environment or import
`DISPLAY`, `WAYLAND_SOCKET`, preload variables, proxy settings or secrets. Defaults
are `/usr/bin:/bin` and `C.UTF-8`. Package launchers needing more must receive a
deliberately reviewed provisioning change, not a second renderer implementation.

Both trusted-image and the single installation-owned untrusted-web root are
replaceable browser/runtime state, excluded from V0 backup/restore authority.
The isolated untrusted-web profile may retain ordinary cookies/local storage/site
preferences across Chromium restarts and reboots. It is never shared with
administration or trusted-image state, and is not split per Source/site. A fresh
installation or replacement host may start clean. V0 provides no third-party login
provisioning, session portability, credential injection, cookie import/export,
profile copying or login scripting. Long-lived protected credentials await #29.
#64 owns lifecycle; #26/#11 own managed cache/root cleanup and disk bounds. No
incognito-emulating deletion machinery is added. Never remove a root while its
controller is running or its lock is held.

Configured web URLs accept HTTP/HTTPS, including deliberate loopback/private/LAN
services and public/share identifiers. Source forms perform no DNS/HTTP/browser
probe. Normal Chromium TLS validation stays enabled: no certificate bypass or
application trust store. A LAN service without a browser-trusted certificate can
use intentionally configured HTTP. Keep full URLs out of renderer/public logs
and diagnostics because share identifiers can be non-public. See the
[Sources guide](sources.md#web-url-sources) for configuration. #82 implements
configuration/resolution; #83 supplies the renderer capability and #84/#31 own
physical evidence.

`WebRenderer(ContentSurfaces(...))` accepts only a resolved `WebTarget`. Its caller
must serialize `show(target, cancelled=...)`, `clear(cancelled=...)` and `stop()`
outside requests/transactions. Global runtime switching must retire any different
active content first. The renderer borrows only the coordinator's untrusted owner.
Each presentation blanks (five seconds by default), then navigates freshly (15
seconds). Recoverable browser startup/control/navigation/crash failure gets at most
one fresh-browser retry after cleanup. Invalid input, cancellation, unavailable
session and cleanup uncertainty receive no retry. A loaded page continues its own
scripts/network; a rendered HTTP error response is still page content.

Failed loads and clear failures retire to compositor black. A cleanup failure
retains ownership and blocks activation until `stop()` succeeds; report this
honestly rather than claiming black if retirement failed. Public renderer errors
contain only `invalid_target`, `unavailable`, `cancelled` or `cleanup_failed`, plus
secondary cleanup status. Never serialize traceback locals or controller internals.
No periodic reload, new worker, Flask browser endpoint or Scene controls are added.
Deterministic tests cover the renderer policy and existing Chromium navigation
seam. No local Chromium/labwc installation is available for a real-browser smoke;
this supplies no physical Raspberry Pi/HDMI evidence for #84.

CDP is bound to `127.0.0.1` with port `0` (OS selection). Under the private root,
`DevToolsActivePort` is bounded to 512 bytes, validated as an owned regular file,
and removed before launch and after cleanup. It is runtime metadata, never a
persisted port setting. WebSocket messages are capped at 256 KiB, the receive
queue at eight frames, and command-time event buffering at 128 messages. No
HTTP discovery redirects, proxy use, CDP forwarding through Flask, or remote
control endpoint is supported. Same-account/root access remains a host trust
boundary for #29; profile permissions are not isolation from the appliance user.

The owning runtime component serializes calls and supplies its shutdown Event's
`is_set` as `cancelled`. Defaults are 10 seconds for start/restart, 15 for navigate,
and 5 for blank, including startup when needed; explicit deadlines must be finite
and within `(0, 120]` seconds. Protocol waits check cancellation/process exit every
50 ms; a startup connect/handshake may take up to 250 ms per bounded phase.
Cleanup adds at most a 100 ms CDP close, 250 ms TERM grace and 500 ms leader-reap
allowance. No other component may reap this controller's child. Failed operations
stop the browser; failed cleanup retains authority and blocks replacement.

`ChromiumError.reason` distinguishes `invalid_spec`, `invalid_url`,
`session_unavailable`, `startup_failed`, `cdp_startup`, `cdp_failed`,
`navigation_timeout`, `navigation_failed`, `browser_exited`, `cancelled` and
`cleanup_failed`. A secondary cleanup failure preserves the primary reason and
sets `cleanup_failed=True`. Do not serialize exception locals or private controller
state: URLs/frame tokens, cookies, debugging endpoints and browser output are not
public diagnostics. On a healthy browser, blank success means its fixed black
document loaded; it does not establish HDMI visibility or physical panel standby.

Deterministic process/CDP tests and a real loopback WebSocket peer validate the
software boundary. No suitable local Chromium/labwc installation was available
for #64's optional browser smoke. Actual packaged Chromium plus private native
Wayland, sandboxing, profile permissions and process-group behavior must still be
validated on both Ubuntu Server and Raspberry Pi OS under #66. This is not a
physical Pi/GPU/HDMI/4K support claim.


## Shared surface and overlay capability (#65)

The ordinary-Python APIs are `ContentSurfaces.select/reconcile/stop`,
`MpvSurfaceProbe.ensure_started/stop`, `Overlay.start/show/hide/receive/stop`,
and `InputChannel.start/receive/stop` under `postcardscene.graphics`. They are
serialized capabilities for the later runtime owner, not an installed player
loop or end-user controls. Use one `WaylandSession` and the existing #63 output
policy. Supply the two #64 Chromium contexts and the inert mpv probe to one
`ContentSurfaces`; do not independently start those owners thereafter. Consumers
may navigate the selected browser through #64's existing API.

Selection stops the old group before starting another class. `none` leaves
labwc's black background. A crash check retires the failed owner without
restarting it or revealing an older surface. A stop failure retains authority
and blocks a replacement until cleanup succeeds. The runtime must poll
`reconcile` and call `stop` on shutdown/cancellation; systemd remains outer
process-tree supervision. Do not launch helpers through labwc autostart.

### Host launch and toolkit boundary

#26 provisions absolute, trusted host launchers. `MpvSurfaceProbe(session,
command=(absolute_mpv_launcher, ...))` accepts package invocation words, never a
shell command or caller-provided mpv options. Launchers must exec or wait and
keep descendants in the owned process group. The probe uses `--no-config`,
`--gpu-context=wayland`, `--vo=gpu`, immediate force-window/idle/fullscreen,
no border, no audio, no default bindings/OSC and no scripts. It loads no media
and opens no mpv IPC. The [mpv windowing options](https://mpv.io/manual/stable/)
provide a surface proof, not video/codec/hardware-decode support.

`Overlay(session, system_python=absolute_system_python)` executes the packaged
`overlay_helper.py` directly with the provisioned system Python. Ubuntu 24.04's
GTK3 baseline is sufficient: `python3-gi`, `gir1.2-gtk-3.0`,
`gir1.2-gtklayershell-0.1` and their system libraries. Raspberry Pi OS uses the
same helper. Do not install PyGObject through uv or require GTK4-layer-shell.
Package/launcher provisioning remains #26; no system installation command is
introduced here. All children receive #62's explicit native-Wayland environment,
not inherited application secrets or X11 state.

### Overlay and local input protocol

The helper writes `ready` after toolkit/layer-shell initialization and a Wayland
round trip, initially with neither input surface mapped. The parent sends closed
JSON state with exactly `enabled`, `visible`, `paused`, `seekable`, `audio`, `muted`
and `volume`. The first five fields are booleans, `muted` is boolean/null, and
`volume` is integer 0–100/null. Updates reply `updated` after a Wayland round trip.
No title/path/URL/HTML/progress payload is needed. Frames are ASCII and bounded to
1024 bytes including newline; duplicate/unknown keys and wrong types are rejected.
The original idempotent `show`/`hide` commands (`shown`/`hidden` replies) remain
for controlled diagnostics; `stop` exits. Nonblocking output, malformed input,
EOF and parent disconnect cause retirement, with bounded process-group cleanup.

Helper output and the private socket accept only `activity`, `previous`,
`toggle_pause`, `next`, `seek_back_10`, `seek_forward_10`, `toggle_mute`,
`volume_down_10`, `volume_up_10`. The inherited-pipe parent retains at most 16
events. `InputChannel` binds `postcardscene-input.sock` with mode 0600 in #62's
validated 0700 runtime directory, rejects existing endpoints and removes only its
owned inode. Unknown/oversized datagrams fail closed. The emitter has a 100 ms
send bound and prints no raw diagnostics.

The visible compact dark panel is separate from a transparent bottom-edge hotspot
of 8 logical pixels, with zero exclusive zone. Hide unmaps the panel; the hotspot
maps only while enabled and hidden. Pointer enter/click/touch **at that bottom
edge** reveals controls. Arbitrary motion over Chromium/mpv does not. Suppression
disables both surfaces. No full-screen transparent surface intercepts content.

`graphics/labwc/playback-keybindings.xml` is the version-1 production snippet for
#26 to install inside `rc.xml`'s keyboard element, preserving its inert desktop
fallback binding. Each entry invokes `/opt/postcardscene/venv/bin/postcardscene-input-emitter`
directly with one fixed action: Left/Right navigate, Space toggles pause,
Ctrl+Left/Right seek ±10 seconds, M toggles mute, Up/Down adjust volume ±10.
No shell, environment expansion or user-configurable command is used. The existing
`probe_keybinding` now emits only `activity` for disposable W-F12 smoke tests.
These reserved keys are appliance playback policy; arbitrary web-page keyboard
interaction is not a V0 goal. No installed compositor configuration is modified
by runtime or Flask.

`runtime.controls.Controls(playback, overlay, channel, stop_event)` starts once
and joins within five seconds. It alternates bounded socket/pipe reads on one
thread, sends changed state only as needed, and forwards to #89 rather than
executing media work. Controls start hidden and auto-hide five monotonic seconds
after local activity, including while paused; snapshot refresh does not prolong
visibility. Capability actions unavailable in current playback return small local
feedback; unknown mute/volume values are not guessed. Ordinary helper/input
failure retires both endpoints and reports unavailable; cleanup uncertainty is
fatal and wakes shared host cancellation. #93 still owns actual construction,
shutdown integration and runtime restart on fatal failure.

### Diagnostics and evidence limits

`CapabilityError.reason` and `CapabilityStatus.public_diagnostics()` expose
fixed reasons only: readiness/stopped, invalid specification, unavailable
session/input/content, startup failure, helper exit, protocol failure, timeout,
cancellation or cleanup failure. `cleanup_failed` preserves a primary failure
when retirement also fails. Content diagnostics include `active_content` and
per-class last-probe availability; these are capability observations, not live
physical-display health. Overlay status describes helper availability, while
`visible` records the acknowledged panel state. No window lists, URLs, titles,
raw input, subprocess output, GTK objects or environment dumps are exposed.

A controlled Ubuntu 24.04 x86-64/WSL software smoke used labwc 0.7.1/wlroots
0.17.1 with the headless pixman backend, GTK3 3.24.41 and gtk-layer-shell 0.8.2,
mpv 0.37.0, and cached Chrome for Testing 151.0.7922.34 through the unchanged
#64 launcher/controller (including its sandbox policy).
System packages were unpacked privately for this test, not installed as an
appliance. Both isolated browser contexts and mpv created surfaces on the same
session; retirement restored the black compositor background. Overlay show/hide
was checked with compositor screenshots. Both browser contexts rendered a
controlled local HTTP page. A compositor virtual-pointer click activated the
visible probe above each content class; W-F12 injected by wtype exercised the
actual labwc Execute -> fixed emitter -> private channel path with each content
owner focused. Test input injection was confined to the disposable compositor;
none of that machinery is shipped in the application. This proves software
pointer/button routing, not a physical touchscreen. The cached testing browser
shows its own testing banner; it is not a production package recommendation.
This is software evidence only.

Deterministic tests cover stop-before-start ordering, retained cleanup authority,
crash-to-none behavior, same-class reuse, native launcher flags/environment,
configured buffer-backed mpv readiness, capped malformed/timeout helper protocol,
cancellation, process-group descendant cleanup and private typed input/config.
No tests claim physical touch, Pi/HDMI/4K, acceleration, audio or packaged-distro
installation support. #66 must supply that evidence; #92 supplies software controls and #93 wiring,
#6 video, #7 web policy and #10 panel power/protection.

### Supervised video capability (#73)

`video_player.MpvController(session, command=(absolute_mpv_launcher, ...))` is the
real video owner beneath `ContentSurfaces`; `MpvSurfaceProbe` remains inert.
Supply the existing #62 session and a trusted exec/wait launcher retaining its
process group. Prepare one borrowed #72 FD after coordinator construction, then
select VIDEO. Preparation owns a duplicate, so the safe-open context may end.
Switching content retires the video through the existing black interstitial rule.

The child inherits only pinned media and private socketpair IPC descriptors.
The [mpv JSON IPC contract](https://mpv.io/manual/stable/#json-ipc) supports inherited
`--input-ipc-client=fd://N`; no listener or runtime socket provisioning is needed.
Poll the serialized controller's status regularly to observe playing/ended/failed;
startup is bounded by its timeout/cancellation and requires authoritative load.
Stop retires prepared/active authority with bounded group TERM/KILL. A retained
`cleanup_failed` indicates unresolved process authority (including possible kernel
I/O stalls); do not activate replacement content until retirement succeeds.
Diagnostics contain only state, fixed reason and cleanup marker.

Poll `snapshot()` on demand for immutable live position/duration and seek capability.
`pause()`/`resume()` are idempotent; relative seeks accept ±3600 seconds, and absolute
seeks require a known positive duration and reject targets outside it. Unknown duration
disables the scrubber; non-seekable media disables all seeking. Operations default to
a one-second deadline, support cancellation, and raise fixed `PlaybackError` reasons.
Stop cancels pending IPC and retires the child. Successful load has no hidden playback
time limit; the owner observes real EOF. See the [architecture transport contract](architecture/rendering.md#active-video-transport-74).

Pass #71 Widget JSON to `prepare(fd, configuration=...)`. Omission is silent:
`audio_enabled=false`, volume 50. Opt-in audio starts unmuted at its configured
integer volume (0–100), capped at 100. `snapshot().audio` reports enabled/available,
transient mute/volume, a fixed reason and automatic versus explicit device policy.
`mute()`, `unmute()` and `set_volume(integer)` are bounded active-audio operations;
changes are transient and reset on a newly prepared video.

Trusted host callers may construct `MpvController(..., audio_device=...)` with
`None`/`auto` or one bounded mpv `driver/device` value; see the [architecture audio contract](architecture/rendering.md#active-video-audio-75).
There is no device setting/UI or enumeration. A missing track plays silently;
[mpv's null-output fallback](https://mpv.io/manual/stable/#audio) preserves silent
video when an intended device cannot open, without selecting another real device.
The snapshot reports `device_unavailable`, not ready audio. No passthrough,
normalization or advanced processing is enabled.

EOF now reaps the child and preserves `ended` for the owner. Scene retirement and
future #10 sleep/off integration must call stop; process termination is the final
silence boundary. `cleanup_failed` means silence is unconfirmed and replacement
content must remain blocked. #8 controls, #10 panel/schedules and physical
Pi/HDMI/audio/codec/hwdec evidence (#66/#76) remain separate.
Deterministic helper-process tests prove FD/IPC/lifecycle behavior, not physical
playback support. No installed mpv/Wayland smoke is available in this development
environment.


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

## Panel power capabilities (#111)

The ordinary-Python CEC, DDC and signal capabilities are implemented independently
of the running appliance. #113 now constructs them when trusted host configuration
enables the live panel group below. Display management is described below;
schedule/playback connection and static-protection enforcement remain pending.

The future installation/doctor owner (#26) must provide the fixed trusted tools
`/usr/bin/cec-ctl`, `/usr/bin/ddcutil` and `/usr/bin/wlopm`, with normal non-root
device/group permissions. No package installation, device discovery provisioning,
udev rules or root application requirement is introduced here.

- CEC needs a trusted canonical `/dev/cecN` selector and an already-configured
  adapter with transmit support and one non-TV logical address. Missing selection
  is unavailable; the application never guesses an adapter. It sends Image View
  On first for wake and Standby for sleep, then queries TV power status. Physical
  on/off is reported only from an actual matching stable status reply.
- DDC optionally consumes a trusted positive display number. Without it, multiple
  responsive displays are unavailable/ambiguous. Brief discovery must identify one
  I2C bus and D6 must be readable before control. The capability retains that bus,
  including across standby query loss, and writes only D6 on `0x01` or off `0x04`.
  It disables user tool configuration and implicit set verification, then performs
  its own bounded readback. A successful set with missing readback is unknown;
  a later explicit wake can address the same retained bus without rediscovery.
- Signal power needs the existing private Wayland session and the exact ready
  #63-selected HDMI output, plus working `wlopm` output-power protocol support.
  Fresh `wlopm` output-mode readback confirms only compositor signal on/off.
  The query also requires a matching output-power mode event from a private
  `WAYLAND_DEBUG=client` trace; missing/failed events cannot masquerade as off.
  **Signal off never confirms physical monitor/TV standby.** The future owner must
  keep intentional signal sleep from conflicting with output reconciliation.

Each command has a three-second deadline, 16 KiB capture limit and 50 ms
cancellation polling, followed by at most 250 ms kill/reap cleanup. Public results
contain only closed state/reason values; raw output, stderr, selectors, display
serials and environment values are discarded from status. Stderr is discarded
except query-only Wayland trace, which shares the same capture cap and is parsed
privately before being discarded. A timeout or uncertain request must not trigger
another backend. Cleanup failure is separately marked,
blocks further calls on that capability, and requires restart through the later
runtime owner. These direct tools must not be replaced with shell wrappers.

Tests cover fake protocol responses and controlled child-process failure,
cancellation and reaping. They do not prove tool/package compatibility, physical
standby/wake, Raspberry Pi/HDMI support or device permissions. #26/#116 retain those
installation and physical evidence gates; CEC/DDC availability can disappear on
standby or HDMI disconnect without establishing the physical power state.


## Live panel runtime and local control (#113)

`postcardscene-runtime` optionally starts exactly one panel coordinator alongside
catalog refresh. Trusted `POSTCARDSCENE_CONFIG` accepts:

| Key | Accepted value / default |
| --- | --- |
| `PANEL_POWER_RUNTIME_ENABLED` | Exact bool; default `False` |
| `CEC_DEVICE` | Optional canonical `/dev/cecN`, N 0–9999 |
| `DDC_DISPLAY` | Optional integer 1–9999, never bool |
| `DISPLAY_CONNECTOR` | Optional exact `HDMI-A-N`, N 1–9999, using #63 rules |

`DISPLAY_CONNECTOR` is graphics authority and is valid without panel enablement.
The output monitor and enabled signal backend receive the same validated value.
CEC/DDC selectors without explicit panel enablement fail configuration.
Enabled hosts may omit
selectors: CEC is then unavailable, DDC requires one unambiguous display, and
signal uses #63's unambiguous HDMI selection. Invalid values fail before workers
start. These inputs never come from SQLite or web requests. The existing
`/run/postcardscene-wayland` remains graphics authority. Managed installation #26
must configure/enable panel power on supported appliances; default disabled keeps
source-checkout catalog work usable and reports panel capability unavailable.

The host directory `/run/postcardscene` must already exist, owned by the non-root
runtime user, mode 0700 or 0750, under trusted ancestors. Application code creates
only `panel-control.sock`, mode 0600 or 0660 respectively; for 0750 it inherits
the directory GID explicitly. A separate web user must have that group as its
primary GID for SO_PEERCRED authorization. #26 provisions users/groups and tools;
this change installs nothing. Existing endpoints (including stale sockets) fail
startup and are never unlinked automatically. An operator must establish the old
owner is stopped before removing a stale endpoint. Normal cleanup removes only
the socket inode this process created.

Clients connect with Linux AF_UNIX `SOCK_SEQPACKET` and send exactly one UTF-8 JSON
packet, maximum 1024 bytes, with exactly these keys:

```json
{"version":1,"action":"status"}
```

The other two actions are `test_on` and `test_off`; no additional arguments or
configurable test duration exist. The listener handles one client at a time with
100 ms receive/send bounds. Oversized/malformed/unauthorized packets are rejected;
clients should use a bounded timeout and treat absent socket, timeout or disconnect
as unavailable. Responses fit one packet of at most 1024 bytes:
`version`, `outcome` (`accepted|unavailable|rejected|cleanup_failed`) and `status`.
Status contains configured/lifecycle/backend, operating/protection/test and effective
intent, physical/signal observation, evidence, fixed reason and signal-sleep
ownership. No selectors, serials, tool output, environment or schedule data leave
the runtime. Accepted means submitted/read, not confirmed physical transition;
read status to observe convergence. Signal-only evidence always leaves physical
state unknown.

Tests use #112's fixed five-second transient intent. Test wake can override ordinary
operating sleep but is rejected during protection; Test sleep temporarily overrides
active intent. Expiry restores current policy without changing settings, schedules
or protection. Normal service shutdown stops requests, joins the panel owner, then
continues catalog cleanup without requesting on/off. Panel unavailability degrades
locally; cleanup uncertainty stops the host and returns nonzero for supervisor
recovery. The control plane is independently restartable.

Signal selection reuses #63 inside the existing panel pass without mode reapply.
The selected output survives intentional off. The #124 output monitor and signal
backend share one non-reentrant `DisplayMutationGuard`: acquire in cancellable
slices of at most 50 ms, then recheck `intentional_signal_sleep` before any
reconciliation. Signal holds that guard through every bounded wlopm operation
and readback; CEC/DDC remain outside it. A pass already in progress finishes
before signal off; a waiting pass skips until retained sleep/wake authority clears.
Neither poll waits, DB work nor wake delays hold the guard.

Startup reserves panel control and starts the coordinator before the monitor.
Shutdown sets shared cancellation, stops control requests and joins the monitor,
panel and catalog within existing bounds. Missing session, no display and ordinary
tool degradation are nonfatal; uncertain output-tool cleanup stops the runtime
with nonzero exit rather than replacing the owner. In-process
`host.display_status` retains the last immutable #63 snapshot (initially `None`);
`host.output_monitor_status` separately reports fixed suspension without rewriting
that snapshot as on/off/ready. No new status IPC, renderer or labwc configuration
is introduced. Wlopm changes power without requiring a mode reapply. #104 retains schedule/playback suppression wiring, #115
static protection, and #116 physical support evidence. These local
software checks make no physical standby/wake or Raspberry Pi support claim.

## Display settings, status and tests

Open authenticated **Display** to save the backend, wake delay (0–30 seconds), and
maximum static dwell (300–14400 seconds) together. Invalid input preserves the
previous complete policy. Runtime/socket unavailability does not prevent saving.
Automatic tries usable CEC -> DDC/CI -> signal capabilities in fixed order;
explicit HDMI-CEC, DDC/CI or Signal only selection is strict with no silent fallback.
Uncertain command authority never permits stacking another backend. Device selectors
and permissions remain trusted host provisioning, outside this form.

Wake delay follows an actual wake transition before presentation release; already
confirmed on does not incur that delay. Maximum static dwell is a non-disableable
safety ceiling, separate from playback dwell. Playback Pause and schedule Keep
active do not disable panel protection. Live static-content/stalled-video enforcement
remains #115 and schedule/playback integration remains #104; saving these settings
does not implement either. The runtime reads saved policy on its bounded cadence;
a save sends no runtime command.

**Live panel status** is a page-load snapshot; use **Refresh live status** for a new
bounded read. The runtime's configured/selected backend may lag saved policy.
Physical state confirmed means physical CEC/DDC evidence. Signal output on/off
leaves physical panel state unknown. Unknown/degraded status never proves standby;
blank playback is not power evidence. Unavailable/not-configured runtime, malformed
responses and socket failures show **Runtime panel status unavailable**, without
local paths or raw errors.

**Test wake** and **Test sleep** submit fixed five-second diagnostic intents through
the local socket. Submission does not prove a transition succeeded; refresh status
to observe convergence or backend degradation. Wake may override ordinary operating
sleep but never protection sleep. After five seconds current policy resumes. Tests
change no durable settings or schedule and are not manual scheduling overrides.
Rejection cannot bypass protection; cleanup-authority failure requires runtime
recovery and is not retried by the web process. Flask invokes no hardware tools.
These controls make no physical-support claim; #116 retains hardware evidence.

## Installed read-only diagnostics (#141)

Run `/opt/postcardscene/venv/bin/postcardscene-doctor` as an authorized local host
administrator (normally via `sudo`); add `--json` for the same ordered, immutable
check results in JSON. No configuration-path, command, impersonation or repair
options are accepted. Exit **0** means all applicable checks are ready; **1** means
degraded/unavailable checks need attention; **2** means an installation identity or
configuration inconsistency (or invalid invocation); **3** is an internal command
error. `backup: not_applicable (not_implemented)` is expected until #27 lands.

Fixed check identifiers cover release/wheel identity, exact #140 assets and
conflict-record structure, config/two-UID/key/DB/sidecar/IPC permissions, independent
service states, DB/schema/catalog health, #123 storage thresholds, web serving,
graphics and executable panel-tool prerequisites. No secrets, hostnames, media
paths, URLs, symlink targets, raw configuration or tool/error output are printed.
Each check has a five-second deadline and isolated failure; systemd output is
limited to 16 KiB and three allowlisted properties. Storage warning/critical
thresholds remain exactly #123's policy.

Doctor never opens the installed database through SQLite, which could create
sidecars even for a read-only query. It checks a private, disposable copy of only
the main DB and existing WAL (64 MiB combined maximum), rejecting files that
change while copied, then reuses `Database.check()` and persisted catalog health.
Private 0700 scratch under `/tmp` and 0600 copies are removed after each check,
including a check timeout. This is diagnostic sampling, **not a backup**. A busy,
oversized or unreadable DB reports unavailable; catalog summaries cap Sources at
1,000. Installed DB/WAL/SHM and signing-key authority are never repaired or created.

To avoid executing host code, configuration inspection accepts literal Python
assignments only. Valid dynamic Python remains service-owned and reports
`config_not_inspectable`; doctor does not execute it. Serving classification uses
the existing #131 validator without a listener. Graphics checks use the existing
session and read-only output probes only under the runtime UID; outside that UID,
including root, `capability_unavailable` is expected. Tool readiness means executable
DAC prerequisites only, not device access or physical panel support.

Doctor does not restart services, migrate, rescan media, read signing-key bytes,
parse journals, mutate hardware or establish HDMI/4K/acceleration evidence. Use
#140/#142 for provisioning/reinstall ownership, #144 for recovery-backed updates,
#27 for backup/restore, and #66/#116/#127 for physical validation. Review individual
service results independently; web failure does not imply runtime failure.
