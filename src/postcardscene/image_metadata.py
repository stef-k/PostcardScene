"""Header-only image presentation metadata; no pixel decode or retained EXIF."""

import warnings
from dataclasses import dataclass

from PIL import Image

from postcardscene.filesystem_source import MediaEntry, resolve_item_path


@dataclass(frozen=True)
class ImageMetadata:
    entry: MediaEntry
    status: str
    width: int | None = None
    height: int | None = None
    orientation: str | None = None


def inspect_image(kind, configuration, policy, entry):
    """Re-resolve and compare freshness after inspection, outside DB transactions.

    Resolution retains #22's documented point-in-time race limitation. Mounted
    callers must execute this exclusively in their disposable child process.
    """
    try:
        path = resolve_item_path(kind, configuration, policy, entry.relative_path)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                width, height = image.size
                # Use header EXIF only: PNG.getexif() otherwise calls load()
                # to discover trailing chunks, decoding the entire image.
                if Image.Image.getexif(image).get(274) in (5, 6, 7, 8):
                    width, height = height, width
        info = resolve_item_path(
            kind, configuration, policy, entry.relative_path
        ).stat()
        if (info.st_size, info.st_mtime_ns) != (entry.size_bytes, entry.mtime_ns):
            return ImageMetadata(entry, "pending")
        if width <= 0 or height <= 0:
            return ImageMetadata(entry, "error")
        orientation = (
            "square"
            if width == height
            else ("portrait" if height > width else "landscape")
        )
        return ImageMetadata(entry, "ready", width, height, orientation)
    except Exception:
        # Decoder/plugin and path failures are item-local; never expose raw paths
        # or use a metadata failure as evidence of filesystem absence.
        return ImageMetadata(entry, "error")
