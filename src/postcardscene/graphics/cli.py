"""Exec-only labwc launch and bounded systemd readiness; no restart supervisor."""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from postcardscene.graphics import WaylandSession

ASSETS = Path(__file__).parent


def launch(session: WaylandSession) -> None:
    session.validate_directory()
    # A private directory makes labwc's automatic first socket deterministic.
    # Refuse stale/foreign state; only systemd owns directory cleanup.
    if any(session.runtime_directory.glob("wayland-*")):
        raise ValueError("Wayland runtime directory is not clean.")
    environment = session.client_environment()
    environment.pop("WAYLAND_DISPLAY")  # Never nest inside an existing compositor.
    environment.update(
        PATH="/usr/bin:/bin",
        HOME=str(session.runtime_directory),
        XDG_CONFIG_HOME=str(ASSETS),
        XDG_CONFIG_DIRS=str(ASSETS),
        WLR_BACKENDS="drm,libinput",
        WLR_XWAYLAND="/usr/bin/false",
        XKB_DEFAULT_OPTIONS="srvrkeys:none",
    )
    # Only seat provisioning is inherited. No desktop hooks, tokens, DISPLAY,
    # loader overrides, or session-bus activation environment are propagated.
    seat_backend = os.environ.get("LIBSEAT_BACKEND", "logind")
    if seat_backend not in ("logind", "seatd"):
        raise ValueError("Unsupported seat backend.")
    environment["LIBSEAT_BACKEND"] = seat_backend
    for key in ("XDG_SESSION_ID", "XDG_SEAT", "XDG_VTNR"):
        if key in os.environ:
            environment[key] = os.environ[key]
    os.execve("/usr/bin/labwc", ["labwc", "-C", str(ASSETS / "labwc")], environment)


def wait_ready(session: WaylandSession, compositor_pid: int) -> int:
    deadline = time.monotonic() + 10
    while True:
        status = session.inspect(compositor_pid=compositor_pid)
        if status.available:
            return 0
        if (
            not status.runtime_directory_valid
            or status.reason in ("compositor_exited", "unexpected_peer")
            or time.monotonic() >= deadline
        ):
            print(json.dumps(status.public_diagnostics()), file=sys.stderr)
            return 1
        time.sleep(0.1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("launch", "wait", "status"))
    parser.add_argument("--pid", type=int)
    args = parser.parse_args()
    if args.action == "wait" and args.pid is None:
        parser.error("wait requires --pid from the compositor service MainPID")
    try:
        session = WaylandSession()
        if args.action == "launch":
            launch(session)
        elif args.action == "wait":
            return wait_ready(session, args.pid)
        else:
            status = session.inspect(compositor_pid=args.pid)
            print(json.dumps(status.public_diagnostics()))
            return 0 if status.available else 1
    except (OSError, ValueError):
        print("Graphical session operation failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
