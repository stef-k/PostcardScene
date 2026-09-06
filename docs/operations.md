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
mpv/surface/overlay proof; #63 owns output selection and hotplug. None is added here.

A controlled Ubuntu 24.04 x86-64/WSL smoke used distro labwc 0.7.1 and wlroots
0.17.1, extracted into a temporary directory, with headless/pixman backends and
the packaged appliance configuration. It answered the real sync probe, loaded
no default key/mouse bindings, exited zero on SIGTERM, and became unavailable
after exit. A 1280x720 capture confirmed a black background with the default
pointer; pointer visibility/overlay behavior remains #65. This is software/session
evidence only; headless backends are not
production launch options. Pi ARM64, unattended boot/logind/seatd authority,
HDMI/4K, input behavior and both-distro physical evidence remain with #66.
