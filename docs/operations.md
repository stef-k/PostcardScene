# Appliance operations

This document records implemented operating contracts. Managed installation,
updates and production control-plane serving remain with #26/#29. The templates
below are versioned provisioning inputs, not an installer or a claim of a
physically validated release.

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
session-only. The runtime executable does not yet start output monitoring.

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
Pi/HDMI/4K/hotplug claims remain #66. Panel standby/wake stays with #10; its later
runtime integration must suspend this reconciliation while intentionally disabling
outputs. Renderer lifecycle and shared surfaces remain #64/#65.

## Isolated Chromium control (#64)

The implemented controller lives in `postcardscene.graphics.chromium`. It is not
yet started by the runtime CLI: #58 owns image-adapter integration, #7 full web
URL/session policy, #65 shared Chromium/mpv/overlay surfaces, and #8 playback.

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

Trusted-image root contents are disposable/replaceable and excluded from backup
authority. This controller reuses its dedicated user-data directory across process
restarts but promises no cookie persistence or credential recovery. #7 decides
intentional web-session retention; #26/#11 own managed cache/root cleanup and disk
bounds. Never remove a root while its controller is running or its lock is held.

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
