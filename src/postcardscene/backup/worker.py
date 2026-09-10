"""Internal fixed-operation worker; not an additional console script/service."""

import json
import os
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from . import archive, capture, destination


def execute(operation, path, progress):
    if operation == "verify":
        return asdict(destination.verify(path, progress))
    if operation == "list":
        return [asdict(entry) for entry in destination.list_backups(path, progress)]
    if operation != "create":
        raise ValueError("Unknown operation")
    with tempfile.TemporaryDirectory(
        prefix="postcardscene-backup-", dir="/tmp"
    ) as temp:
        scratch = Path(temp)
        capture.capture(scratch, progress)
        built = archive.build(
            scratch, datetime.now(timezone.utc), version("postcardscene"), progress
        )
        return asdict(destination.publish(built, path, progress))


def main():
    os.umask(0o077)
    last = 0.0

    def progress():
        nonlocal last
        now = time.monotonic()
        if now - last >= 0.1:
            print("progress", flush=True)
            last = now

    try:
        result = execute(sys.argv[1], Path(sys.argv[2]), progress)
        print(json.dumps({"result": result}, separators=(",", ":")), flush=True)
        return 0
    except Exception:
        print('{"error":"backup_failed"}', flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
