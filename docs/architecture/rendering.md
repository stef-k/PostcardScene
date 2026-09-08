# Rendering

Image delivery/presentation, video/audio, scene rendering, untrusted web content and live-update transport.

Read the [architecture entry point and map](../architecture.md) first. Together,
the overview and linked subsystem documents form the architecture authority.

Session, controller and surface ownership are in [Graphics and panel](graphics-and-panel.md); playback execution is in [Runtime and scheduling](runtime-and-scheduling.md).

## Image behavior

### Image Widget semantics and selection (#56)

`postcardscene.image_selection` is the ordinary-Python, DB-only semantic seam for
`image` and `portrait_image_pair`. `validate_image_configuration` accepts an object
with only optional `fit`: `{}` normalizes to `{"fit": "contain"}`, and the only
values are `contain` and `cover`. Other keys/values are rejected. Generic domain
JSON validation remains structural. Presentation uses a fixed neutral black
background and centered object position: contain preserves the whole image with
unused black area; cover fills the region with centered cropping. Widget JSON
contains no duration, transition, selection/shuffle/history, crop coordinates,
color-profile or panel-safety configuration.

`resolve_image_context(session, widget_id)` requires an existing enabled image
Widget referencing an existing enabled `local_directory` or `mounted_directory`
Source. Invalid contexts raise `ImageSelectionError` without catalog mutation.
Within the caller's short transaction, `get_selected_image` looks up canonical
`(source_id, relative_path)` under that Widget, returning `None` for absent,
mismatched or ineligible media. `list_selected_images` returns an immutable page
plus its last row ID as continuation, or `((), None)` at exhaustion. Both queries
resolve context on each call. Only same-Source images with metadata `ready`,
positive presentation dimensions and a normalized portrait/landscape/square
orientation are eligible; video and pending/error metadata are excluded.

Pages accept 1–500 items and a nonnegative `after_id`. The existing
Source/type/orientation index supports three bounded orientation queries; merging
at most three pages avoids sorting a whole remaining Source catalog. Row IDs are
only keyset continuation details, never media identity or playback ordering.
The schema and indexes remain unchanged at `0008_catalog_requests`.

Frozen `SelectedImage` records contain only Source ID, canonical relative path,
expected `size_bytes`/`mtime_ns`, presentation width/height and normalized
orientation. Frozen `ImageFrame` records hold one or two selected images plus
normalized fit. Neither retains an ORM object/session, absolute path, bytes,
Scene identity or playback state. Later file delivery must re-resolve current
Source authority and recheck freshness when opening; a snapshot grants no lasting
filesystem authority. Selection performs no storage probe, scan, read or decode.

Catalog/Pillow metadata readiness is **not proof of Chromium format support**.
There is no browser-format extension filter in selection. #57 owns browser
readiness/decode-failure handling, #58 integrates that result with the real #31
controller, and #59 owns the physically evidenced format/color/quality matrix.
An eligible image may fail browser decoding safely without catalog deletion.

### Automatic portrait grouping and runtime ownership

`build_image_frame(widget_kind, fit, first, lookahead=None)` returns
`(frame, consumed_count)`. The caller supplies already-selected candidates in its
own runtime order. Ordinary `image` always consumes one. `portrait_image_pair`
pairs only two distinct canonical identities with portrait orientation and
consumes two; otherwise it presents the first singly and consumes one. Landscape,
square and final unpaired portraits remain visible. Duplicate identity never
pairs, even when freshness differs. Lookahead remains caller-owned and unconsumed
on single fallback. There are no aspect-ratio heuristics or image-owned iterator,
push-back buffer, cursor, shuffle or history.

#8 defines consecutive candidates through its ordered/shuffled stream and advances
by the consumption count. It owns display-step history, dwell execution,
Previous/Next, Play/Pause and common auto-hiding controls. Scene duration and
Sequence overrides remain #19/#20 configuration. Shared transitions remain #8;
#57 may use a simple fixed image-local fade without another Widget option.
#10 owns static-dwell/panel protection, including while paused.

### Trusted image frame delivery (#57)

`capture_image_source(session, frame)` captures a frozen DB-only `ImageSource`
for a validated one/two-image `ImageFrame` and one enabled filesystem Source.
Capture immediately before `start_image_frame(frame, source, policy)`; the snapshot
is short-lived launch authority, not a live subscription to Source edits. No ORM
object or Source-path I/O crosses into the parent delivery operation.

Each frame owns one disposable `spawn` helper bound to `127.0.0.1` on an ephemeral
port. Its unpredictable token authorizes only the fixed page, asset `0` (and `1`
for a pair), and empty ready/failed POSTs. Exact routes, Host/origin checks and
hashed-inline CSP restrict browser authority; there is no general media API,
Flask service, `file:` access, persistent cache or playback-control surface.

All local/mounted Source access occurs inside the helper. `open_image_item`
rechecks #22 authority, pins directories with descriptor-relative no-follow opens,
rejects special files, and compares opened size/mtime to selection before serving.
Mounted access also reuses #24's deepest NFS/NFS4/CIFS mount guard and checks the
opened descriptor's mount identity to refuse local mountpoint fallthrough.
Original compressed/profile bytes stream from that descriptor in bounded 64 KiB
chunks with HTTP backpressure, without Pillow decoding or conversion.

The fixed black, centered HTML page uses one viewport image or two equal vertical
columns, `object-fit: contain|cover`, and `image-orientation: from-image`. It waits
for all image decodes before asserting ready, or asserts failed on load/decode
failure. The first valid terminal callback wins; byte transfer alone is not ready.
`handle.wait_ready()` consumes the closed progress/terminal protocol and reaps the
helper. Always close the handle (or use it as a context manager). Startup and
waiting enforce a trusted finite positive lack-of-progress timeout and prompt
caller cancellation. Close uses bounded terminate/join/kill/join; Linux
uninterruptible kernel sleep can still prevent timely final reaping.

`ImageDeliveryError.reason` distinguishes invalid context, unsafe/stale item,
unavailable Source, timeout, helper/protocol failure, presentation failure and
cancellation without raw diagnostics or catalog mutation. #8 owns skip/fallback
and global controls. Controlled HTTP tests prove this protocol only: #58 must
prove real supervised Chromium navigation/readiness and frame retention after
helper exit; #59 owns actual format/color/quality evidence.

### Presentation and quality boundaries

Chromium/HTML/CSS is the V0 image compositor selected by #5; mpv remains the video
engine. #57 implements trusted disposable image delivery and the HTML/CSS frame; #31
owns the production graphics session, Chromium supervision and common overlay/input
mechanism; #58 adapts image frames to that controller. No renderer is added by #56.

Image presentation must respect EXIF orientation, preserve original bytes/profile
data and useful 1080p/validated-4K quality without ordinary recompression, and bound
malformed, huge, unreachable or unsupported-image load/decode failures. #59 must
document actual tested formats, color behavior and physical quality limitations
before claiming supported playback. No speculative user-facing playback guide is
created ahead of that evidence.

## Video and audio behavior

Issue #71 implements `postcardscene.video_selection`, an ordinary-Python,
DB-only semantic/query seam. Generic `domain.create_widget` JSON validation stays
structural. `validate_video_configuration` accepts only kind `video` and an object
with optional `audio_enabled` (real boolean, default `false`) and `volume`
(integer 0–100, default 50; booleans rejected). Unknown keys are invalid. Volume
may remain configured while audio is disabled without implying sound. Mute is
transient active-player state; intended audio-device selection is a trusted
host/runtime concern, not Widget JSON. Duration, seek increments, codec/hwdec
policy, Scene dwell, Sequence state and panel safety are not Widget options.

`resolve_video_context` requires an enabled video Widget referencing an existing
enabled local or mounted filesystem Source. `get_selected_video` looks up canonical
`(source_id, relative_path)` identity; `list_selected_videos` returns a tuple of
snapshots and a row-ID keyset continuation, with default 100 and maximum 500 items.
Both use the common catalog, exclude other Sources and non-video items, and need
no image metadata or `duration_ms`. Missing/disabled/invalid context raises
`VideoSelectionError`; absent/ineligible media returns `None`. Malformed paths
retain the shared filesystem validator's typed `InvalidSource` error; pagination
bounds raise `ValueError` as in the catalog.

Immutable `SelectedVideo` carries only `source_id`, canonical `relative_path`,
nonnegative integer `size_bytes` and integer `mtime_ns` (including pre-epoch values).
It carries no ORM/session, absolute path, file descriptor, bytes or playback state.
Selection does no filesystem scan/open, reconciliation, decoder probe or catalog
mutation. #72 revalidates and pins current file authority as described below.
Catalog video classification identifies candidates, not actual mpv/container/codec
support; unsupported media remains catalog knowledge. Progress/duration comes
from the active player without catalog-wide duration indexing.

#8 owns order/shuffle/history, Previous/Next, global Play/Pause and transport UI;
row-ID pagination here defines none of those behaviors. #72–#75 own file authority,
mpv and active-player controls/audio. End-user playback is not implemented by #71;
physical format/HDMI/hwdec claims remain gated by #66/#76.

### Pinned video file authority (#72)

Call `video_file.capture_video_source(session, selected)` in a short transaction
immediately before opening; it revalidates the current enabled filesystem Source
and copies its identity/configuration without storage I/O. End the transaction,
then use `with open_video_item(selected, source, policy) as fd:`.
The immutable #71 selection is freshness state, not file authority. Capture is
point-in-time; callers must recapture for every new pin after Source changes.

The context yields one borrowed read-only, non-inheritable-by-default integer FD.
It checks Source identity, canonical relative path, trusted PathPolicy authority,
recursive policy, no-follow descendants and regular-file size/mtime freshness.
The existing image stream API retains the same shared opener and mount checks.
Rename/replacement after pin cannot redirect the FD; in-place writes by an external
owner are not prevented, and equal-size/equal-mtime content changes cannot be
distinguished by the catalog freshness contract. Original media remains
external/unowned and is never copied into PostcardScene.

Mounted opens run entirely in a fresh spawned helper, including path resolution,
open/fstat, current network coverage and the pinned FD's actual mount identity.
A private Unix socket transfers the already-open FD using SCM_RIGHTS, never a
pathname for later reopening. Progress resets the configurable ten-second default
idle timeout. Cancellation is polled during waiting. Every outcome closes socket
and partial FD copies and uses bounded terminate/join/kill/join cleanup; a kernel
task that resists reaping reports helper failure, preserving an existing primary
failure. No mount worker/service or fallback to local mountpoint content is added.
Local opens remain synchronous with cooperative cancellation between operations.

`VideoFileError.reason` is only `invalid`, `unavailable`, `timeout`,
`cancelled` or `helper`; diagnostics omit paths and raw OS/child details.
The FD is trusted player plumbing only: do not close the borrowed FD, publish it
in Flask/status, or persist it. The #73 controller duplicates this borrowed FD
during preparation, so the safe-open context can close independently. No player
is launched by #72, and physical NAS/codec/HDMI support remains unproven.

### Supervised video process (#73)

`video_player.MpvController` consumes the same Wayland session and implements the
`ContentSurfaces` VIDEO owner contract. Construct the coordinator first, then
`prepare(fd)` with a borrowed #72 pinned descriptor. Preparation duplicates the
read-only regular-file FD; callers may then exit the safe-open context. Only one
prepared/active item is allowed. Stop clears preparation and is idempotent.

Every prepared item starts one fresh process with only the media and private
socketpair endpoint in `pass_fds`. Media uses `fd://N`; JSON IPC uses inherited
`--input-ipc-client=fd://N`, with no filesystem socket or network listener.
Launch argv is shell-free, native Wayland, fullscreen and borderless. Host config,
scripts, terminal/default input, OSC, external-file discovery and media references
are disabled. Audio follows the explicit #75 policy below. No playlist progression,
codec/hwdec selection or runtime scheduling is added.

Startup waits boundedly for `file-loaded` and a correlated fixed JSON handshake.
Messages are capped at 8 KiB and processing at 32 steps per poll. Unknown or invalid
protocol fails closed. `end-file` with reason `eof` after load means `ended`;
other endings, crash, timeout and cancellation produce fixed playback failures.
The serialized owner polls `status`/surface reconciliation regularly; there is no
background monitor or automatic retry. EOF reaps the child while retaining observable
`ended` state until owner retirement.
Stop closes parent FDs and sends bounded group TERM/KILL, retaining the child handle
and `cleanup_failed` if reaping fails. Replacement content remains blocked through
ContentSurfaces until cleanup succeeds. Launchers must retain descendants in their
process group; systemd remains the outer runtime-owner crash boundary.

### Active video transport (#74)

`MpvController.snapshot()` returns an immutable `PlaybackSnapshot`: lifecycle state,
fixed reason, cleanup marker, optional position/duration in seconds, seekability,
and absolute-seek capability. These are live private IPC properties, never catalog
`duration_ms` updates. Nonfinite, negative or missing numbers become `None`;
absolute seek requires seekability and a known positive duration. Unknown seekability
is false; an unavailable/malformed pause property fails the operation explicitly.

`pause()`/`resume()` set the actual pause boolean idempotently. `seek_relative(seconds)`
accepts finite signed deltas within ±3600 seconds; `seek_absolute(seconds)` requires
0 through the known duration inclusive. Booleans and nonnumeric values are rejected.
Out-of-range absolute targets are rejected, leaving future UI clamping to #8.
Both use mpv keyframe seek modes for ordinary playback and return a refreshed snapshot;
relative boundary behavior follows mpv, without editing-grade accuracy guarantees.

All operations share the controller lock and correlated private IPC owner. The
one-second default deadline covers each complete control operation, including lock
acquisition and snapshot refresh; callers may supply a finite timeout up to 60 seconds
and cancellation callback. Stop signals cancellation before acquiring the lock.
Timeout, cancellation and protocol/process failures during IPC retire the child through #73;
cancellation before lock acquisition sends nothing and leaves cleanup to the owner.
Retirement invalidates queued commands even if the controller is reused.
mpv command rejection raises `PlaybackError("command_failed")` without restarting.
Invalid seeks and unavailable capabilities also leave the player intact. Inactive
controls raise `not_active`, or `cancelled` after retirement; terminal snapshots remain
readable with unavailable progress/capabilities. No operation prepares another item.

Successfully loaded video has no maximum playback duration: it reaches real EOF
unless its owner retires or pauses it. These are #6 media capabilities beneath #8's
global Play/Pause. Previous/Next are #8 display-step navigation, never mpv playlist
commands. No UI/history or panel-safety policy is introduced.

### Active video audio (#75)

`prepare(fd, configuration=...)` consumes #71's validated video Widget JSON:
`audio_enabled=false`, integer `volume=50` by default, bounded 0–100. Disabled
players launch with `--audio=no` and never select an audio device. Enabled players
start unmuted with explicit initial volume, `--volume-max=100`, passthrough and
ReplayGain disabled; user config/scripts remain disabled. No normalization,
equalizer, dynamic-range processing or other advanced audio controls are added.

The controller's optional trusted `audio_device` constructor argument accepts
`None`/`auto` or one ASCII mpv `driver/device` value of at most 256 characters
(letters, digits and `_./:,=-`, no leading dash). It is a single shell-free
argument, never Widget JSON, a persistent setting, enumeration or a mixer command.
Explicit devices do not fall back to another real output: mpv's
`--audio-fallback-to-null=yes` preserves silent video on device-open failure.
Automatic selection permits mpv's normal output choice. Final HDMI/device policy
and physical support remain gated by #66/#76.

`PlaybackSnapshot.audio` is immutable: configured `enabled`, live `available`,
optional `muted`/bounded `volume`, fixed `reason`, and `device_policy` (`auto` or
`explicit`). Scalar selected-audio-track and current-output IPC properties establish
`no_track`, `device_unavailable` (including null output), or `ready`; readiness is
mpv output evidence, not proof that a physical speaker is audible. Missing output
means unavailable; malformed properties fail the bounded control operation.
Inactive/disabled/degraded audio exposes no mute/volume capability or raw device data.

`mute()`/`unmute()` set the actual boolean idempotently; `set_volume(value)` accepts
only integers 0–100, rejecting booleans and invalid values with `invalid_volume`.
These use #74's serialized deadline/cancellation/retirement boundary and refresh
live state. Unavailable audio raises `audio_unavailable`; inactive players follow
#74's `not_active`/`cancelled` contract. Adjustments write no database state and
never carry into another prepared video's initial policy.

Stop, failure, cancellation, EOF and content retirement close IPC and reap the
process group; successful mute is never the final silence authority. Failed cleanup
retains degraded ownership and reports `cleanup_failed`, not confirmed silence.
#8 must retire video before changing display steps; #10 must use this same stop
primitive for sleep/off. Neither runtime integration nor panel policy is added here.

Video playback should use mpv where practical because codec support, hardware acceleration, control, and failure isolation benefit from a dedicated player.

V0 should support:

- local/network video files through the common catalog
- full-duration playback where configured
- bounded load/start timeouts
- controlled skip/failure behavior
- supervised player recovery
- hardware decoding where the validated runtime supports it
- an honest supported codec/container boundary

V0 does not require automatic transcoding.

Audio is explicit appliance state rather than an accidental mpv default. The system must define at least:

- audio enabled/muted
- bounded configured volume when enabled
- intended audio output where practical, including HDMI on validated hardware
- safe behavior with no audio track/device
- immediate silence when the owning scene is skipped/stopped or the panel is put to sleep

No orphan audio may continue after a scene transition, player restart, or scheduled display-off state.

## Scene composition and rendering

Rich scenes should primarily be rendered with Chromium using HTML/CSS/JavaScript.

This provides a flexible composition surface for:

- CSS Grid/Flexbox layouts
- overlays
- cards/panels
- typography
- maps/web content
- smooth transitions
- responsive scaling between 1080p and 4K
- lightweight animations
- image presentation effects where appropriate

Chromium is the V0 image compositor selected by #5; image content consumes the
shared #31 graphics/controller boundary rather than a dedicated native stack.

The runtime/player service coordinates the active scene and Chromium/mpv processes.

## Web-content isolation

Configured web pages are untrusted renderer content even when an administrator
chose the URL. #82 implements `postcardscene.web_selection` independently of Flask
and Chromium. `validate_web_source(kind, configuration)` requires `web_url` and
exactly `{"url": "..."}`. It trims outer whitespace, bounds the result to 8192
characters, rejects controls/DEL, embedded whitespace, backslashes, userinfo,
missing hostnames and invalid/zero ports, and accepts absolute HTTP/HTTPS only.
Paths, case, percent-encoding, query strings and fragments are preserved.
Loopback, private/link-local addresses and LAN/mDNS hostnames are deliberately
allowed for configured local services. Validation does no DNS, HTTP, reachability
or browser work. HTTPS retains normal Chromium certificate validation: no TLS
bypass or application-managed trust store; deliberately configured LAN HTTP is
supported when a service lacks a browser-trusted certificate.

`validate_web_configuration(kind, configuration)` requires `web_view` and exactly
`{}`. Generic domain JSON remains structural; semantic callers validate before
writes. `resolve_web_target(database, widget_id)` owns one short read transaction,
requires the current enabled Widget and enabled referenced `web_url` Source,
validates both configurations and returns a frozen `WebTarget(source_id, url)`.
Missing, disabled, mismatched or invalid state raises sanitized `WebSelectionError`.
The snapshot contains no ORM/session authority; resolve anew for every display
step and never keep a transaction open during browser/network work. URL is omitted
from the target repr; do not emit full URLs in public diagnostics or renderer logs.

Public/share-display URLs, including share identifiers in path/query, are supported
content configuration. V0 has no generic username/password/API-key/token fields,
userinfo, generated credential headers/cookies/JavaScript, cookie import/export,
profile copying, DOM login scripting, OAuth automation or authenticated-page
provisioning. Protected third-party credentials require a later #29-owned design.

Use one installation-owned `untrusted_web` Chromium profile root, separate from
trusted-image and administration cookies/secrets. Ordinary cookies, local storage
and site preferences may persist through browser restarts and host reboots within
that installation. There are no per-Source/per-site profiles. This is replaceable
browser/runtime state, excluded from V0 backup/restore authority; a replacement
host or fresh install may begin clean. Persistence promises neither third-party
login provisioning nor session portability. #64 owns profile/process lifecycle;
no profile wipe/copy machinery is added to emulate incognito.

Scene duration and Sequence overrides own dwell, executed later by #8. Each new
web presentation navigates freshly; V0 has no web-specific duration or periodic
reload option. Third-party pages may continue their own scripts/timers/network;
future Pause holds Scene progression without freezing web execution. #82 launches
no browser and adds no Scene/Sequence execution. #84's physical Pi/browser support remains gated on #31.
Production CDP stays private to local controller authority and is not a Flask API.

#83 implements synchronous `web_renderer.WebRenderer(surfaces)` with
`show(WebTarget, cancelled=...)`, `clear(cancelled=...)` and `stop()`. The caller
serializes these calls outside Flask/DB transactions. `ContentSurfaces.operation`
lends its exact untrusted controller and retains cleanup authority; it refuses
another active content class or pending retirement. #8 still owns global switching
through the coordinator. Renderer clear/stop never activate or retire other classes.

Each show validates the detached target, loads #64's fixed black document (default
five-second bound), then performs a fresh navigation with a 15-second bound.
Success means #64 main-document load completion; normal rendered HTTP error pages
remain content. There is no DOM inspection, injected control UI or background loop.
Startup/control/navigation/crash failure permits at most one fresh-browser retry,
after retirement through #64/#65. Cancellation, invalid input/configuration,
unavailable session and cleanup uncertainty are not retried. Invalid replacement
input also retires prior web content. Clear blanks; failed blank/navigation retires
to compositor black. Stop is bounded/idempotent. Cleanup failure retains authority
and prevents subsequent activation until explicit cleanup succeeds; black cannot
be guaranteed when the OS refuses retirement.

`WebRendererError` exposes only `WebFailure` (`invalid_target`, `unavailable`,
`cancelled`, `cleanup_failed`) and a secondary `cleanup_failed` boolean. It retains
no URL/target or successful-page state. #8 owns subsequent skip/fallback decisions;
#84 remains physically gated, and these software contracts claim no Pi/HDMI support.

## Live updates

Some scenes may benefit from server-to-renderer updates without full reloads, especially later Wayfarer live-location views.

Use the simplest transport that satisfies the required update cadence. Ordinary authenticated HTTP polling is preferred where it is adequate because PostcardScene is a small local appliance and the control plane does not otherwise need an async-first web architecture.

If a concrete V1/V2 requirement demonstrates that server push materially improves behavior, evaluate the smallest suitable mechanism at that time, such as SSE or WebSockets. Do not preselect Flask-Sock, Socket.IO, an ASGI migration, a message broker, or other event infrastructure without evidence that the simpler path is insufficient.

The renderer transport is distinct from the control-plane/runtime IPC boundary: live state remains authoritative in the runtime/player process, so changing browser transport must not collapse the process separation defined elsewhere in this architecture.
