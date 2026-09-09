"""Bounded read-only diagnostics for a managed PostcardScene installation."""

import argparse
import tempfile
from functools import partial
from pathlib import Path

from . import CHECKS, Check, Report, data, live, metadata
from .bounded import inspect_bounded


def collect():
    results = []
    for identifier in CHECKS:
        if identifier == "backup":
            results.append(Check(identifier, "not_applicable", "not_implemented"))
            continue
        if identifier in {"database", "catalog"}:
            # Only disposable private copies are writable, never installed state.
            with tempfile.TemporaryDirectory(
                prefix="postcardscene-doctor-", dir="/tmp"
            ) as scratch:
                function = partial(data.database_check, directory=Path(scratch))
                results.append(inspect_bounded(function, identifier))
            continue
        if identifier.startswith("service_"):
            function = live.service
        elif identifier.startswith("storage_"):
            function = live.storage
        elif identifier.startswith("panel_"):
            function = live.panel_tool
        elif identifier == "graphics":
            function = live.graphics
        elif identifier == "web_config":
            function = data.web_config
        else:
            function = metadata.inspect
        results.append(inspect_bounded(function, identifier))
    return Report(tuple(results))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="structured")
    args = parser.parse_args()
    try:
        report = collect()
    except Exception:
        print("Doctor internal error.")
        return 3
    print(report.render(structured=args.structured))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
