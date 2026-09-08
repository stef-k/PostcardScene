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
The packaged runtime still performs catalog work only; #93 will wire playback and
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
