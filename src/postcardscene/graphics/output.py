"""One HDMI display policy and cancellable reconciliation; no panel power control."""

from collections.abc import Iterator
from dataclasses import asdict, dataclass, replace
from threading import Event

from postcardscene.graphics import WaylandSession
from postcardscene.graphics.output_probe import (
    CONNECTOR,
    Connector,
    Mode,
    Output,
    ProbeError,
    read_connectors,
    read_outputs,
    run_command,
)


@dataclass(frozen=True)
class DisplayStatus:
    session_available: bool
    state: str
    reason: str
    connector: str | None = None
    manufacturer: str | None = None
    product: str | None = None
    current_mode: Mode | None = None
    desired_mode: Mode | None = None
    current_scale: float | None = None

    def public_diagnostics(self) -> dict:
        """Only selected connector, numeric modes and EDID manufacturer/product codes."""
        return asdict(self)


def select_connector(
    connectors: tuple[Connector, ...], override: str | None
) -> Connector | None:
    if override is not None and (
        not isinstance(override, str) or CONNECTOR.fullmatch(override) is None
    ):
        raise ProbeError("invalid_connector_override")
    eligible = tuple(connector for connector in connectors if connector.connected)
    if override is not None:
        eligible = tuple(
            connector for connector in eligible if connector.name == override
        )
        if not eligible:
            raise ProbeError("connector_mismatch")
    if len(eligible) > 1:
        raise ProbeError("ambiguous")
    if not eligible:
        return None
    # Card prefixes are lost in Wayland names; duplicate DRM identity is unsafe.
    if sum(connector.name == eligible[0].name for connector in connectors) != 1:
        raise ProbeError("connector_mismatch")
    return eligible[0]


def choose_mode(modes: tuple[Mode, ...]) -> Mode:
    safe = tuple(mode for mode in modes if mode.refresh <= 60.2)
    groups = (
        tuple(
            mode
            for mode in safe
            if (mode.width, mode.height) == (3840, 2160) and mode.refresh >= 59.8
        ),
        tuple(
            mode
            for mode in safe
            if (mode.width, mode.height) == (1920, 1080) and mode.refresh >= 59.8
        ),
        tuple(mode for mode in safe if mode.preferred),
        tuple(mode for mode in safe if mode.current),
    )
    for candidates in groups:
        if candidates:
            return min(
                candidates,
                key=lambda mode: (
                    abs(mode.refresh - 60),
                    not mode.preferred,
                    not mode.current,
                    -mode.width,
                    -mode.height,
                    mode.refresh,
                ),
            )
    raise ProbeError("no_safe_mode")


def _mode_arguments(mode: Mode, modes: tuple[Mode, ...]) -> list[str]:
    matches = [candidate for candidate in modes if candidate.selector == mode.selector]
    if len(matches) != 1:
        if mode.preferred and sum(candidate.preferred for candidate in modes) == 1:
            return ["--preferred"]
        raise ProbeError("ambiguous_mode")
    width, height, millihertz = mode.selector
    return ["--mode", f"{width}x{height}@{millihertz / 1000:.3f}Hz"]


def _matches(output: Output, desired: Mode, preferred_selector: bool) -> bool:
    current = output.current
    return (
        output.enabled
        and current is not None
        and current.selector == desired.selector
        and (not preferred_selector or current.preferred)
        and output.scale == 1.0
        and output.position == (0, 0)
        and output.transform == "normal"
    )


def _selected_output(outputs: tuple[Output, ...], connector: Connector) -> Output:
    output = next((output for output in outputs if output.name == connector.name), None)
    if output is None:
        raise ProbeError("connector_mismatch")
    return output


def reconcile_display(
    session: WaylandSession,
    *,
    connector_override: str | None = None,
    stop_event: Event | None = None,
) -> DisplayStatus:
    """Inspect fresh state, apply at most once and verify; expected failures are data."""
    status = DisplayStatus(False, "degraded", "session_unavailable")
    if not session.inspect().available:
        return status
    try:
        environment = session.client_environment()
    except (OSError, ValueError):
        return status
    status = replace(status, session_available=True)
    try:
        connectors = read_connectors()
        connector = select_connector(connectors, connector_override)
        # With no physical display, tool availability must not make boot fatal.
        if connector is None:
            return replace(status, state="no_display", reason="no_display")
        status = replace(
            status,
            connector=connector.name,
            manufacturer=connector.manufacturer,
            product=connector.product,
        )
        output = _selected_output(read_outputs(environment, stop_event), connector)
        status = replace(
            status, current_mode=output.current, current_scale=output.scale
        )
        desired = choose_mode(output.modes)
        status = replace(status, desired_mode=desired)
        mode_arguments = _mode_arguments(desired, output.modes)
        preferred_selector = mode_arguments == ["--preferred"]
        if not _matches(output, desired, preferred_selector):
            run_command(
                [
                    "--output",
                    connector.name,
                    "--on",
                    *mode_arguments,
                    "--pos",
                    "0,0",
                    "--transform",
                    "normal",
                    "--scale",
                    "1",
                ],
                environment,
                stop_event,
            )
            # A successful command is not evidence of accepted output state.
            output = _selected_output(read_outputs(environment, stop_event), connector)
            status = replace(
                status, current_mode=output.current, current_scale=output.scale
            )
            if not _matches(output, desired, preferred_selector):
                raise ProbeError("apply_failed")
        return replace(status, state="ready", reason="ready")
    except ProbeError as error:
        reason = str(error)
        return replace(
            status,
            state="ambiguous" if reason == "ambiguous" else "degraded",
            reason=reason,
        )


def monitor_display(
    session: WaylandSession, stop_event: Event, *, connector_override: str | None = None
) -> Iterator[DisplayStatus]:
    """Blocking snapshots for a runtime-owned consumer; one interruptible second between polls.

    The owner must consume promptly and share its shutdown Event. No thread,
    compositor, renderer, database or service is created by this component.
    """
    while not stop_event.is_set():
        status = reconcile_display(
            session, connector_override=connector_override, stop_event=stop_event
        )
        if stop_event.is_set():
            return
        yield status
        if stop_event.wait(1.0):
            return
