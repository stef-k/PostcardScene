"""Header-only image presentation metadata; no pixel decode or retained EXIF."""

import os
import warnings
from dataclasses import dataclass

from PIL import Image

from postcardscene.filesystem_source import MediaEntry, open_image_item


@dataclass(frozen=True)
class ImageMetadata:
    entry: MediaEntry
    status: str
    width: int | None = None
    height: int | None = None
    orientation: str | None = None


def inspect_image(kind, configuration, policy, entry):
    """Inspect one pinned file and compare freshness outside DB transactions.

    Mounted callers execute this exclusively in their disposable child process.
    """
    try:
        with (
            open_image_item(
                kind,
                configuration,
                policy,
                entry.relative_path,
                entry.size_bytes,
                entry.mtime_ns,
            ) as stream,
            warnings.catch_warnings(),
        ):
            if kind == "mounted_directory":
                from postcardscene.mounted_source import _check_open_mount

                _check_open_mount(stream)
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(stream) as image:
                width, height = image.size
                # Use header EXIF only: PNG.getexif() otherwise calls load()
                # to discover trailing chunks, decoding the entire image.
                if Image.Image.getexif(image).get(274) in (5, 6, 7, 8):
                    width, height = height, width
            info = os.fstat(stream.fileno())
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
