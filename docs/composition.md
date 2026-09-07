# Composition configuration

PostcardScene uses **Source → Widget → Scene → Sequence**. A Source describes
where content comes from; a Widget defines how that content is presented; a Scene
places a Widget on the display; a Sequence orders Scenes and controls progression.

The authenticated control interface currently manages Sources, all four V0 Widget
kinds, and single Scenes. Sequence editing, active playback selection and playback
controls are not available in this UI yet. Saving these forms configures durable
state; it does not start playback, preview content or test content availability.

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
these forms do not activate a runtime or expose Sequence/default-dwell settings.

Existing `split_vertical` and `split_horizontal` Scenes remain intact and are
listed as **Not executable in V0 — reserved for future composition**. There is no
split editor or automatic conversion. Create a single Scene for V0 instead.

## Disable and delete

Disable through the edit form to preserve configuration and references. Disabling
a Widget does not disable its Scene or Source; disabling a Scene does not alter its
Widgets or Sequence memberships.

A Widget referenced by any Scene cannot be deleted. A Scene referenced by any
Sequence cannot be deleted, even when the referring object is disabled. Remove or
reassign the reference first. Sequence membership editing is not yet available in
the control UI, so a blocked Sequence reference remains blocked here.

Unreferenced single Scenes can be deleted from their edit page. Future split
Scenes have a delete action on the list, subject to the same restriction. Deleting
a Scene removes only its placements; deleting a Widget preserves its Source and
media catalog. Neither action deletes original media.
