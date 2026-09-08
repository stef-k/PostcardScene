# Graphics and panel

Wayland/labwc, output policy, Chromium/shared surfaces, overlay/input and physical panel protection.

Read the [architecture entry point and map](../architecture.md) first. Together,
the overview and linked subsystem documents form the architecture authority.

Content and web isolation semantics are in [Rendering](rendering.md); execution and operating intent are in [Runtime and scheduling](runtime-and-scheduling.md).

## Linux graphics session

The live #31/#62 contract selects native Wayland with labwc for Raspberry Pi
4/5-class ARM64. Ubuntu Server LTS ARM64 on Raspberry Pi (24.04 and 26.04 where
packages are available) and Raspberry Pi OS 64-bit are both primary V0 targets.
One graphics implementation serves both; X11, alternate compositors and full
desktop/display-manager dependencies are outside V0. Windows is outside V0,
while platform-neutral application layers retain their existing boundaries.

#62 packages an isolated appliance labwc configuration and systemd/PAM templates
beneath `postcardscene.graphics`. One dedicated non-root graphical/runtime user
owns the service. The default unattended local-VT session uses pam_systemd/logind
and libseat; seatd is an explicit provisioning alternative. Distro differences
are limited to package/user/seat/device provisioning owned by #26. No managed
installer, application service or renderer is introduced here.

The exec-only launcher gives labwc a private, systemd-owned 0700 local runtime
directory `/run/postcardscene-wayland`, with the first socket `wayland-0`. It uses
packaged configuration rather than the administrator's desktop profile, rejects
stale socket state, and passes an allowlisted environment. Inert bindings prevent
older labwc from loading desktop defaults. Empty composition clears to black
without a wallpaper utility; native Wayland clients receive no X display.

`WaylandSession` validates runtime-directory ownership/mode and returns explicit
child graphics variables. Its bounded core Wayland sync probe reports compositor
availability separately from process activation and physical display state;
optional MainPID matching rejects a different peer. Public diagnostics contain
only fixed safe vocabulary. Systemd owns restart and bounded TERM/KILL cleanup;
RuntimeHost remains application lifecycle authority. Renderers must be runtime
children, never labwc autostart jobs. No generic supervisor is added.

[Operations](../operations.md#linux-graphical-session-62) owns the service/PAM/seat
provisioning contract, readiness semantics and evidence limits. #63 implements
connector/EDID/mode/hotplug policy below; #64 owns Chromium isolation/control, #65 shared
surfaces/overlay/input capability, and #66 representative physical validation on
both primary distro paths. Package availability and headless Linux smoke do not
prove Pi/HDMI/4K, non-root seat permissions, acceleration or audio support. #31
remains open until those children and its umbrella acceptance contract are met.

### One-display output policy (#63)

`postcardscene.graphics.output.reconcile_display(session, connector_override=...)`
consumes the existing `WaylandSession` readiness and explicit client environment.
Both primary distro paths use the same Linux DRM/wlr-randr code. Only connected
`card*-HDMI-A-*` sysfs connectors are eligible. Zero is normal `no_display`; one
is selected; multiple are `ambiguous` unless a trusted host override names one
exact `HDMI-A-N`. Invalid/disconnected overrides and duplicate card identities
fail closed. The selected name must match a current Wayland output.

Bounded DRM status/EDID reads supply physical presence and manufacturer/product
codes only. `/usr/bin/wlr-randr --json` supplies advertised modes and current
output state. Description, model text, serials and raw EDID are discarded.
JSON/schema/tool failures become fixed degraded reasons, with no X11 fallback.
The command schema and millihertz selector follow
[upstream wlr-randr](https://gitlab.freedesktop.org/emersion/wlr-randr/-/blob/master/main.c).

Policy selects advertised 3840x2160 at 59.8–60.2 Hz, then 1920x1080 in that
class, then preferred/current modes at <=60.2 Hz; otherwise `no_safe_mode`.
Candidates sort by distance to 60, preferred, current, descending width/height,
then refresh. Numeric selectors use millihertz; indistinguishable timings fail
closed unless a unique preferred mode can be controlled with `--preferred`.
Scale is exactly 1, position 0,0, transform normal. No custom timing, high-refresh,
HDR or VRR selection is introduced. Unselected outputs are not reconfigured.

Reconciliation applies at most once, only if needed, using shell-free argv and
a two-second command deadline with capped 256 KiB stdout and discarded stderr.
Fresh JSON must verify the mode identity, enabled state and layout. A command
exit code alone cannot establish readiness. Immutable `DisplayStatus` snapshots
expose selected connector, EDID codes, current/desired mode, scale and fixed
state/reason; no raw environment, tool output or display serial is public.

`monitor_display(session, stop_event, connector_override=...)` is a blocking
snapshot iterator with an interruptible one-second wait after each reconcile.
The later runtime owner consumes it and supplies its existing shutdown Event;
this change starts no runtime thread/service. Tool waits observe cancellation
at most every 50 ms, with a 250 ms kill/reap allowance. Repeated polls recover
from no-display/connect/disconnect/reconnect and failures without busy retry.
No-display skips the unnecessary Wayland output command and leaves labwc alive.
These are software contracts; #66 owns physical HDMI/4K/hotplug evidence and
#10 owns panel standby/wake. Future power coordination must suspend output
reconciliation while intentionally disabling an output so it is not re-enabled.

### Supervised Chromium controller (#64)

`postcardscene.graphics.chromium.ChromiumController` is the single ordinary-Python
browser capability for both distro paths. Its serialized synchronous API is
`ensure_started`, `navigate`, `blank`, `restart` and `stop`, with bounded operations
and caller cancellation predicates. It consumes `WaylandSession.inspect()` and
`client_environment()` directly; browser/control readiness requires no physical
display. No runtime loop, output monitor or renderer adapter is started here.

`ChromiumLaunchSpec` supplies a trusted absolute package launcher and optional
package invocation words, private profile root and small non-secret environment
overlay. Browser options are code-owned: native Wayland/Ozone, kiosk, suppressed
first-run/restore UI, disabled extensions/plugins, no sandbox weakening or broad
file access, and one fixed black data page. There is no default Chromium path,
distro branch or parent-environment inheritance. #26 provisions actual launchers;
#66 must validate each launcher against this same contract.

`BrowserContext.TRUSTED_IMAGE` accepts only explicit-port `http://127.0.0.1/`
caller URLs with an absolute path and no userinfo/fragment. `UNTRUSTED_WEB` accepts
ordinary HTTP/HTTPS URLs without userinfo; #82 defines the application URL and
session policy in [Web-content isolation](rendering.md#web-content-isolation). Caller data/file/script/browser URLs are rejected.
Each root is private, non-root-owned, non-symlinked and context-marked; an existing
personal profile is never adopted. Nested roots and cross-context reuse are
rejected. A profile lock prevents simultaneous owners. The two contexts use
independent process groups, user-data directories and CDP connections. Trusted
state is replaceable, not backup authority; the untrusted-web root follows #82's
persistent-but-replaceable policy in [Web-content isolation](rendering.md#web-content-isolation).

V0 uses loopback CDP with OS-selected ephemeral port, discovered only from fresh,
bounded `DevToolsActivePort` metadata under the dedicated profile. Endpoint host
is constructed as `127.0.0.1`, with proxies disabled and no public control API.
The locked `websockets` synchronous client supplies framing, bounded messages and
queues; the private controller limits commands to version/target discovery,
attachment, page lifecycle/navigation and denying downloads. It selects one
initial black page and fails closed on extra/replaced page targets. No generic
CDP, DOM automation or GPU diagnostic API is exposed.

Navigation follows the [CDP Page contract](https://chromedevtools.github.io/devtools-protocol/tot/Page/):
command acceptance alone is insufficient. A matching main-frame/loader load event
(or same-document event) and fresh frame state must agree. Browser error pages,
downloads, dialogs, timeouts and cancellations cannot count as loaded content.
`blank` verifies the fixed black document. Failures retire the browser to expose
labwc's black background; #57 image decode readiness remains a separate assertion
for #58 to compose. Operations expose only `ChromiumError.reason` and a separate
`cleanup_failed` flag, never URLs/tokens, CDP endpoints, stderr or profile data.

Launch uses `start_new_session=True`, never `preexec_fn`. Cleanup retains the
unreaped leader until the final process-group signal, preventing PID reuse even
when a wrapper exits before its children. TERM receives 250 ms, then group KILL
and bounded leader reaping complete cleanup; failed cleanup retains ownership and
prevents replacement. Systemd remains outer supervision. #65 owns shared surfaces,
#66 real package/Wayland/hardware evidence, and #8 later runtime integration.

### Shared surfaces and transient overlay/input (#65)

`graphics.surfaces.ContentSurfaces` is the serialized content-class ownership
seam. It consumes the existing trusted/untrusted `ChromiumController` instances
and `MpvSurfaceProbe` on the same `WaylandSession`; it duplicates neither browser
nor output policy. V0 permits **one active content surface at a time**. Switching
classes stops the previous process group before starting the replacement; labwc's
black background is the intentional interstitial and failure fallback. Failed
cleanup retains ownership and blocks replacement. Same-class use may reuse its
controller. The later runtime owner polls `reconcile` and owns shutdown; this
capability adds no Scene, scheduling, history or automatic restart loop.

`graphics.mpv_probe.MpvSurfaceProbe` creates only an inert fullscreen native
Wayland GPU window, with audio, default bindings, OSC and scripts disabled. Its
trusted absolute host launcher has code-owned flags and bounded process cleanup.
Readiness requires a configured, buffer-backed Wayland surface commit, not merely
a live process. Probe-only Wayland trace is capped, consumed and discarded. No
media loading, JSON IPC, codec/hwdec or playback controls are implemented.

`graphics.overlay.Overlay` launches the system-Python GTK3/gtk-layer-shell
helper; GI remains outside main application imports and uv dependencies. #92
extends #65 with common Previous/Play-Pause/Next and capability-aware video
seek/audio controls. Closed target-free state travels over inherited ASCII JSON
pipes (maximum 1024 bytes including newline); only closed actions return.
The bottom panel uses OVERLAY with zero exclusive zone and genuinely unmaps on
hide. A separate transparent **8 logical-pixel bottom-edge** surface enables
pointer/touch reveal only while controls are hidden and output is not suppressed.
There is no full-screen hidden interceptor or global pointer observation.

`graphics.local_input.InputChannel` carries the same nine typed actions through
an owner-only runtime-directory socket. The versioned `playback-keybindings.xml`
snippet maps fixed compositor keys directly to one absolute emitter plus one
allowlisted action; #26 owns installation. No shell, raw key stream, DOM/CDP
injection, browser control API or additional renderer authority is involved.

`runtime.controls.Controls` owns one cancellable control/input thread suitable
for #93 integration. It forwards actions to #89's bounded mailbox, reads its
snapshot (including its explicit output-suppression boolean) and owns only chrome
visibility: hidden initially, valid activity reveals,
five monotonic seconds without activity hides even when paused. Status updates
never reset this timer. Suppression disables both panel and hotspot. Modest bounded
polling services both private event sources; only needed state changes reach the
helper. One pending command outcome provides small local feedback without another
queue. Unavailable controls retire locally and playback can continue; uncertain
cleanup sets shared cancellation and a fixed fatal failure for #93. Join is bounded
to five seconds. This does not construct or modify RuntimeHost/content execution.

[Operations](../operations.md#shared-surface-and-overlay-capability-65) records the
protocol, software smoke and limits. #8 owns runtime/control UI integration, #6
real mpv playback, #7 web policy, #10 panel protection, #26 system provisioning,
and #66 representative physical/package evidence on both distro paths.

## Display power management

Stopping playback is not sufficient. During configured sleep periods PostcardScene should attempt to put the physical panel into standby so it is neither a night-time light source nor needlessly active.

Power control remains a separate subsystem from the Linux graphics session.

Preferred methods:

1. HDMI-CEC for compatible TVs.
2. DDC/CI for compatible monitors.
3. DRM/KMS or HDMI-signal control as a fallback where supported.

Settings should allow automatic capability discovery where practical, preferred backend, fallback, wake/handshake delay, test actions, and schedule configuration.

The host/runtime remains alive while the panel sleeps so administration, scheduling, catalog reconciliation, and later provider refresh can continue.

A failed power operation should surface degraded/unknown state rather than falsely claiming the physical panel reached the requested state.

## Burn-in and static-content protection

Panel protection is separate from scheduled sleep.

Potential configurable protections include:

- maximum dwell time for static web/dashboard scenes
- optional subtle position/pixel shifting for appropriate static content
- avoidance of unnecessary permanent overlays
- renderer/player watchdog
- safe blanking if the renderer stalls
- eventual standby after prolonged severe failure

Ordinary changing photography should not be subjected to distracting movement solely for burn-in prevention.

