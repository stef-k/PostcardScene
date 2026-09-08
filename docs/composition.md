# Composition configuration

PostcardScene uses **Source → Widget → Scene → Sequence**. A Source describes
where content comes from; a Widget defines how that content is presented; a Scene
places a Widget on the display; a Sequence orders Scenes and controls progression.

The authenticated control interface currently manages Sources, all four V0 Widget
kinds, single Scenes, Sequences and active playback selection. Saving these forms
configures durable state; it does not start playback, preview content or test
content availability. Actual playback integration (#93) remains unfinished. The #92 local controls
component is implemented separately and awaits that runtime wiring.

## Configure a Widget

First add a Source in **Sources**, then open **Widgets** and choose the explicit
**Add Image**, **Add Portrait pair**, **Add Video** or **Add Web view Widget** link.
Enter a name, select a compatible Source and set the options below. Sources marked
**Disabled** remain selectable: disabling preserves configuration, but disabled
content is not eligible for playback. These selectors show names and kinds;
filesystem paths and web URLs remain in Sources.

| Widget kind | Source kinds | Options |
| --- | --- | --- |
| Image (`image`) | Local or mounted directory | Fit: contain (default) or cover |
| Portrait pair (`portrait_image_pair`) | Local or mounted directory | Fit: contain (default) or cover |
| Video (`video`) | Local or mounted directory | Enable audio: off by default; volume: integer 0–100, default 50 |
| Web view (`web_view`) | Web URL | No Widget-specific options; edit the URL in Sources |

**Contain** shows the whole image with unused space; **cover** fills the region
with centered cropping. Portrait pairing happens automatically within the
portrait-pair Widget using consecutive eligible portrait images. Other eligible
images display singly; no second Scene region or manual media selection is needed.

Video output-device policy belongs to the host/runtime, not the Widget. Widget
forms do not configure selection order, shuffle, duration or raw JSON.

Use **Edit** to change the name, Source, options or enabled state. The kind is fixed:
to replace image behavior with video or web behavior, create a new Widget and
reassign the Scene. A missing compatible Source prevents saving and links back to
Sources; saving never creates a placeholder Source.

## Configure a single Scene

Open **Scenes → Add single Scene**. Enter a name, choose one existing Widget for
the **main** region, optionally enter a duration and choose whether it is enabled.
All current Widget kinds, including disabled Widgets, are selectable. If no Widget
exists, add one before saving.

The layout is fixed to **single**. Editing replaces the one main placement and
Scene fields together; it does not change the selected Widget or its Source.

Leave duration blank for no fixed Scene dwell, or enter an integer **1–86400**
seconds. The timing contract gives a Sequence membership's duration override
precedence over Scene duration. Without either, images/portrait pairs/web use the
application default dwell (initially 30 seconds), while video uses natural
completion. Explicitly timed video finishes at end of file or the duration limit,
whichever comes first. This is progression timing, not panel/burn-in protection;
these forms do not activate a runtime. Configure fallback dwell in Settings.

Existing `split_vertical` and `split_horizontal` Scenes remain intact and are
listed as **Not executable in V0 — reserved for future composition**. There is no
split editor or automatic conversion. Create a single Scene for V0 instead.

## Configure a Sequence

Open **Sequences → Add Sequence**. Enter a name, choose **Ordered** or **Shuffle**,
and add at least one single Scene occurrence. Ordered follows the configured
occurrence order; Shuffle varies the occurrence order during playback without
rewriting configuration. Neither mode saves a playback cursor, shuffle seed or
history. Disabled single Scenes remain selectable and are marked **Disabled**.

The same Scene can appear repeatedly. Each row is a separate occurrence with its
own position and optional duration override; adding a duplicate never merges rows.
Use **Add occurrence**, **Remove occurrence**, **Move Up** and **Move Down** to
prepare the draft, then **Save Sequence** to save all fields and the complete
ordered list together. The ordering buttons do not save; **Cancel** discards the
draft. No dragging or JavaScript is required. Each save may replace occurrence
identities; identities remain stable between saved configuration changes.

Leave an occurrence's duration override blank to defer to Scene timing, or enter
**1–86400** seconds. Timing precedence is **membership override → Scene duration →
default image/portrait-pair/web dwell or natural video completion**. An override
does not change the reusable Scene's duration. Timed video may finish sooner at
end of file.

Split layouts cannot be selected as new V0 memberships. Existing split occurrences
remain visible on the list and in their original edit rows, marked **Not executable
in V0**. They are reserved for future composition. To save an edited Sequence,
explicitly remove or replace every unsupported occurrence with a single Scene;
opening, moving or cancelling the draft never drops or converts stored rows.

## Select active playback and fallback dwell

In **Settings**, choose an **Active Sequence** and **Save active selection**, or
choose **None / Idle** to intentionally select no playback. There is no automatic
first-Sequence fallback. A disabled Sequence may remain selected, but is ineligible
until re-enabled; disabling does not erase selection or its occurrences.

Set **Fallback Scene dwell in seconds** with **Save fallback dwell**. Its default
is **30 seconds**, and accepted values are integers **1–86400**. It applies only
to otherwise untimed image, portrait-pair and web Scenes, not untimed video. This
normal progression setting is separate from panel-protection/static-dwell safety.
Each Settings button saves only its own setting.

The playback worker consumes active/composition changes at its next display-step
or bounded idle reevaluation. Configuration saves do not send commands to the
runtime or launch/stop Chromium, mpv or media; they do not promise an instant
on-screen change. Real content wiring remains #93's responsibility.

## Disable and delete

Disable through the edit form to preserve configuration and references. Disabling
a Widget does not disable its Scene or Source; disabling a Scene does not alter its
Widgets or Sequence memberships.

A Widget referenced by any Scene cannot be deleted. A Scene referenced by any
Sequence cannot be deleted, even when the referring object is disabled. Remove or
reassign the reference first using the owning Scene or Sequence edit form.

Unreferenced single Scenes can be deleted from their edit page. Future split
Scenes have a delete action on the list, subject to the same restriction. Deleting
a Scene removes only its placements; deleting a Widget preserves its Source and
media catalog. Neither action deletes original media.

Deleting a Sequence deletes only that Sequence and its owned occurrences. Scenes,
Widgets, Sources and media are preserved. Deleting the active Sequence clears
selection to **None / Idle**; no replacement Sequence is chosen.

## Local playback controls

The common on-display panel provides **Previous**, **Play/Pause** and **Next**
above images, video and web content. Previous retraces bounded transient playback
history; Next moves forward using the shared planner. Pause holds automatic
progression and remaining dwell: images remain static, web pages keep their own
scripts/timers running, and video pauses. Previous/Next while paused select and
hold the target; a new video starts paused. Restart begins a fresh unpaused epoch.

Controls are hidden initially and hide after **5 seconds** without local activity,
even while paused. Keyboard/button activity reveals and resets the timer; playback
status updates do not. Move the pointer to, click, or touch the **bottom 8 logical
pixels** to reveal the panel. Pointer movement elsewhere on content does not reveal
it. The edge hotspot is disabled while the panel is visible or output is suppressed.

| Key | Action |
| --- | --- |
| Left / Right | Previous / Next |
| Space | Play/Pause |
| Ctrl+Left / Ctrl+Right | Seek back / forward 10 seconds |
| M | Toggle mute |
| Up / Down | Volume +10 / -10, capped at 0–100 |

Seek buttons appear only for seekable active video; mute and volume buttons appear
only when video audio control is available. Unsupported actions do not interrupt
image/web playback. Unknown mute/volume state is unavailable rather than guessed.
The compositor reserves these keys for playback regardless of content focus;
general web-page keyboard interaction is outside V0. There is no remote web
playback API. #26 owns keymap installation and #93 connects the component to real
runtime playback; physical HDMI, keyboard and touch usability remain evidence
gates, not claims established by the software tests.
