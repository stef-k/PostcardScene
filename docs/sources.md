# Sources

Use **Sources** in the authenticated control interface to manage directories of
photos and videos. The catalog records media information; your original files
stay in their directories. Use **Add web Source** for HTTP/HTTPS display URLs.

## Before adding a filesystem Source

The host administrator must configure `MEDIA_ALLOWED_ROOTS` in the trusted Python
configuration file selected by `POSTCARDSCENE_CONFIG`. Web and runtime processes
must use the same configuration. For example, if `/srv/postcardscene-media` is
the intended media location:

```python
MEDIA_ALLOWED_ROOTS = ["/srv/postcardscene-media"]
```

This is an example, not a default. Choose absolute paths appropriate to your host;
the filesystem root `/` is not permitted. After changing host configuration,
restart the web and runtime processes to load it. See the README's
[configuration setup](https://github.com/stef-k/PostcardScene/blob/main/README.md#control-shell-development) and
[runtime setup](https://github.com/stef-k/PostcardScene/blob/main/README.md#runtime-development) for development entrypoints.

Allowed roots are trusted host policy. The Source form shows them as read-only
guidance and cannot change them. With empty or invalid allowed roots, the Sources
list still works, but saving a filesystem Source fails closed. Ask the host administrator to
configure the intended roots; entering a path in the form does not grant access.
Paths must remain within an allowed root, including through existing symlinks.
Relative paths, parent traversal and symlink escapes are rejected.

## Add or edit a Source

1. Log in, open **Sources**, and choose **Add Source**.
2. Enter a recognizable **Name** and select the **Kind**:
   - **Local directory**: a directory on local host storage.
   - **Mounted network directory**: an already-mounted Linux NFS/SMB directory.
3. Enter the absolute **Path** beneath an allowed root. There is no filesystem
   browser; obtain the path from the host administrator if needed.
4. Select **Include subdirectories** for recursive discovery. Leave it unchecked
   for direct-only discovery of files immediately inside the chosen directory.
   Descendant symlink files and directories are not followed.
5. Leave **Enabled** checked to allow catalog refresh, then choose **Save Source**.

For network storage, Linux must already have mounted the share. PostcardScene V0
**does not mount shares, collect NAS credentials here, or run mount/unmount
commands**. A missing network mount is treated as unavailable; a similarly named
local directory is not a substitute for the configured network storage.

Use **Edit** on a Source to change these fields. Renaming alone keeps its catalog
and refresh history and does not queue another refresh. Changing kind, path or
recursive behavior clears the old derived catalog and queues a fresh build if the
Source is enabled. Original media files are not changed.

Clearing **Enabled** preserves catalog knowledge while making the Source inactive
and superseding outstanding refresh work. Disabled Sources cannot be refreshed.
Re-enabling preserves retained knowledge and queues a fresh refresh, since storage
may have changed while the Source was disabled. Creating a disabled Source queues
nothing.

## Refresh the catalog

Choose **Refresh** on an enabled Source. The message **Catalog refresh queued**
means the request was saved, not that scanning finished. Refresh is asynchronous:
the separate runtime process does the storage work, outside the web request.
Enabled creation, relevant edits and re-enabling also queue refreshes after saving.
There is no periodic refresh schedule in V0.

If the runtime is stopped, the request remains queued until it runs again.
Repeated clicks coalesce into pending refresh work rather than requiring a
separate complete scan for every click. Reload **Sources** to see updated persisted
health. If saving succeeds but refresh cannot be queued, the Source remains saved;
retry with **Refresh** after checking database health.

## Understand health and counts

The page reads saved catalog information only. It does not probe storage or ask
the runtime for live status, so an offline NAS does not delay the health page.

| Status | Meaning |
| --- | --- |
| **Never scanned** | No authoritative catalog build has been recorded yet. |
| **Refresh queued** | A saved refresh request is waiting for the runtime to consume it. |
| **Refreshing / interrupted** | An attempt has not completed. It may still be running or may have been interrupted; this is not live progress. |
| **Ready** | The last presence reconciliation completed authoritatively. Image metadata may still be pending or have errors. |
| **Source unavailable** | The latest attempt could not authoritatively traverse storage. Retained entries are previous knowledge. |
| **Needs attention** | The attempt failed with an error and did not establish authoritative storage contents. |
| **Cancelled** | The attempt was cancelled and did not establish authoritative storage contents. |
| **Disabled** | The Source is intentionally inactive; retained catalog knowledge is preserved. |

Disabled status takes priority, followed by an unfinished attempt, a queued
request, and the last recorded result. Older results remain visible alongside the
last attempt and last successful catalog time, shown in the application timezone
selected in **Settings**, with an explicit UTC offset.

Counts show total cataloged items, images, videos, and image metadata
ready/pending/error totals. They are retained catalog counts, **not confirmation
that every file is currently online or playable**. A NAS outage, failed attempt
or cancellation does not prove that unseen media was deleted. Missing entries are
removed only after a complete authoritative storage traversal confirms absence.
When storage recovers, request another refresh to update the catalog.

## Delete a Source

Open **Edit**, read the warning, and choose **Delete Source**. Deletion removes the
Source and its derived catalog, not original files. It never deletes Widgets,
Scenes or Sequences. A Source referenced by a Widget cannot be deleted; the
reference must be reassigned or removed through the owning composition tools
first. This page does not provide a composition editor.

## Web URL Sources

Choose **Add web Source**, enter a **Name** and **Display URL**, set **Enabled**,
and save. Use **Edit** to change the URL/name or enable/disable; deletion uses the
same Widget-reference restriction above. The kind stays Web URL. No Widget is
automatically created, and this page is not a Widget/Scene/Sequence editor.
Web Sources require no `MEDIA_ALLOWED_ROOTS`, have no media catalog, health counts
or Refresh action, and saving does no DNS lookup, HTTP request or browser launch.
The configured URL is visible only after administrator login; mutations use CSRF.

Use an absolute HTTP/HTTPS URL with a hostname, for example
`http://wayfarer.local:8080/display?share=example#map`. Public/share URLs and
loopback/private/link-local/LAN services are deliberately supported. Outer
whitespace is trimmed; paths, case, percent-encoding, query strings and fragments
are preserved. URLs are limited to 8192 characters. Embedded whitespace,
controls/DEL, backslashes, userinfo, invalid/zero ports and non-HTTP(S) schemes
such as file, javascript, data, blob and browser-internal URLs are rejected.
Invalid saves preserve the previous configuration and show form feedback.

HTTPS requires normal browser-trusted certificates: no TLS bypass or custom
application trust store. An intentionally configured LAN service can use HTTP.
V0 has no generic login/password/API-key/token fields or credential injection,
cookie import/export, browser-profile copying, DOM login scripts or OAuth automation.
Share identifiers remain valid URL content; avoid exposing full URLs in logs or
public diagnostics.

One isolated untrusted-web Chromium profile may retain ordinary cookies, local
storage and site preferences across restarts/reboots. It is separate from
administration and trusted images, replaceable, excluded from backup/restore,
and may start clean on a replacement host. This is not supported third-party
login provisioning or session portability; see [Chromium operations](operations/display.md#isolated-chromium-control-64).
Scene/Sequence owns dwell. New presentations navigate freshly; no periodic reload
setting is provided. Pages may run their own timers/scripts even when future
Scene progression is paused. URL management/resolution is implemented by #82;
rendering (#83), composition execution (#8) and physical Pi/browser support
(#84, gated on #31) remain separate work.
