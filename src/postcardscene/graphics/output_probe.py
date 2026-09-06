"""Bounded Linux DRM and wlr-randr I/O for the appliance output policy."""

import json
import math
import os
import re
import selectors
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event

DRM_ROOT = Path("/sys/class/drm")
CONNECTOR = re.compile(r"HDMI-A-[1-9][0-9]{0,3}\Z")
DRM_CONNECTOR = re.compile(r"card[0-9]{1,4}-(HDMI-A-[1-9][0-9]{0,3})\Z")
TOOL = "/usr/bin/wlr-randr"
OUTPUT_LIMIT = 256 * 1024
COMMAND_TIMEOUT = 2.0


class ProbeError(Exception):
    """Contains only a fixed public failure reason."""


@dataclass(frozen=True)
class Mode:
    width: int
    height: int
    refresh: float
    preferred: bool = False
    current: bool = False

    @property
    def selector(self) -> tuple[int, int, int]:
        # wlr-randr rounds the Hz argument to integer mHz before matching.
        return self.width, self.height, math.floor(self.refresh * 1000 + 0.5)


@dataclass(frozen=True)
class Connector:
    name: str
    connected: bool
    manufacturer: str | None = None
    product: str | None = None


@dataclass(frozen=True)
class Output:
    name: str
    enabled: bool
    modes: tuple[Mode, ...]
    scale: float | None
    position: tuple[int, int] | None
    transform: str | None

    @property
    def current(self) -> Mode | None:
        return next((mode for mode in self.modes if mode.current), None)


def _edid_identity(path: Path) -> tuple[str | None, str | None]:
    """Only the base-block manufacturer/product codes; no serial or free text."""
    try:
        with path.open("rb") as source:
            data = source.read(128)
    except OSError:
        return None, None
    if (
        len(data) != 128
        or data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00"
        or sum(data) % 256
    ):
        return None, None
    code = int.from_bytes(data[8:10], "big")
    letters = [(code >> shift) & 31 for shift in (10, 5, 0)]
    if code & 0x8000 or not all(1 <= letter <= 26 for letter in letters):
        return None, None
    return "".join(chr(64 + letter) for letter in letters), data[10:12][::-1].hex()


def read_connectors() -> tuple[Connector, ...]:
    connectors = []
    try:
        with os.scandir(DRM_ROOT) as entries:
            for index, entry in enumerate(entries):
                if index >= 256:
                    raise ProbeError("drm_failed")
                match = DRM_CONNECTOR.fullmatch(entry.name)
                if match is None:
                    continue
                path = Path(entry.path)
                with (path / "status").open("rb") as source:
                    status = source.read(32).strip()
                if status not in (b"connected", b"disconnected"):
                    raise ProbeError("drm_failed")
                manufacturer, product = _edid_identity(path / "edid")
                connectors.append(
                    Connector(match[1], status == b"connected", manufacturer, product)
                )
    except OSError as error:
        raise ProbeError("drm_failed") from error
    return tuple(connectors)


def run_command(
    arguments: list[str], environment: dict[str, str], stop_event: Event | None = None
) -> bytes:
    """Drain a capped pipe, discard stderr, and kill/reap on failure or cancellation.

    No communicate() capture: its memory use would precede the JSON size check.
    The packaged local executable does not spawn children. Kernel-uninterruptible
    process cleanup can exceed the reap bound and is reported as tool_failed.
    """
    if stop_event is not None and stop_event.is_set():
        raise ProbeError("cancelled")
    try:
        process = subprocess.Popen(
            [TOOL, *arguments],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except FileNotFoundError as error:
        raise ProbeError("tool_missing") from error
    except OSError as error:
        raise ProbeError("tool_failed") from error
    try:
        return _capture(process, stop_event)
    finally:
        if process.poll() is None:
            process.kill()
        process.stdout.close()
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired as error:
            raise ProbeError("tool_failed") from error


def _capture(process: subprocess.Popen, stop_event: Event | None) -> bytes:
    deadline = time.monotonic() + COMMAND_TIMEOUT
    captured = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            if stop_event is not None and stop_event.is_set():
                raise ProbeError("cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeError("tool_timeout")
            if not selector.select(min(0.05, remaining)):
                continue
            chunk = os.read(
                process.stdout.fileno(), min(8192, OUTPUT_LIMIT + 1 - len(captured))
            )
            if not chunk:
                selector.unregister(process.stdout)
            captured.extend(chunk)
            if len(captured) > OUTPUT_LIMIT:
                raise ProbeError("oversized_output")
    # A tool can close stdout without exiting. Keep exit waiting cancellable too.
    while process.poll() is None:
        if stop_event is not None and stop_event.wait(0.05):
            raise ProbeError("cancelled")
        if time.monotonic() >= deadline:
            raise ProbeError("tool_timeout")
        if stop_event is None:
            try:
                process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
    if process.returncode:
        raise ProbeError("tool_failed")
    return bytes(captured)


def _number(value, minimum, maximum):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError
    if not minimum <= value <= maximum:
        raise ValueError
    return value


def _mode(value) -> Mode:
    width = _number(value["width"], 1, 16384)
    height = _number(value["height"], 1, 16384)
    refresh = _number(value["refresh"], 1, 1000)
    if type(width) is not int or type(height) is not int:
        raise ValueError
    if type(value["preferred"]) is not bool or type(value["current"]) is not bool:
        raise ValueError
    return Mode(width, height, float(refresh), value["preferred"], value["current"])


def _output(value) -> Output:
    name = value["name"]
    if not isinstance(name, str) or not 1 <= len(name) <= 128:
        raise ValueError
    enabled = value["enabled"]
    if type(enabled) is not bool or not isinstance(value["modes"], list):
        raise ValueError
    if len(value["modes"]) > 512:
        raise ValueError
    modes = tuple(_mode(mode) for mode in value["modes"])
    if sum(mode.current for mode in modes) > 1:
        raise ValueError
    scale, position, transform = None, None, None
    if enabled:
        scale = float(_number(value["scale"], 0.1, 16))
        position = tuple(
            _number(value["position"][axis], -65536, 65536) for axis in ("x", "y")
        )
        if any(type(coordinate) is not int for coordinate in position):
            raise ValueError
        transform = value["transform"]
        if transform not in (
            "normal",
            "90",
            "180",
            "270",
            "flipped",
            "flipped-90",
            "flipped-180",
            "flipped-270",
        ):
            raise ValueError
    return Output(name, enabled, modes, scale, position, transform)


def read_outputs(
    environment: dict[str, str], stop_event: Event | None = None
) -> tuple[Output, ...]:
    raw = run_command(["--json"], environment, stop_event)
    try:
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) > 64:
            raise ValueError
        outputs = tuple(_output(value) for value in values)
        if len({output.name for output in outputs}) != len(outputs):
            raise ValueError
        return outputs
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
        raise ProbeError("malformed_output") from error
