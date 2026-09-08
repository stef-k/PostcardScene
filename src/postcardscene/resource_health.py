"""Read-only filesystem health for explicitly trusted application-owned roots."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ResourceKind = Literal["durable_state", "cache", "runtime"]
HealthState = Literal["healthy", "warning", "critical", "unavailable"]
HealthReason = Literal[
    "sufficient_space", "low_space", "invalid_statistics", "stat_failed"
]
MIB = 1024 * 1024
GIB = 1024 * MIB
# Reject nonsensical host counters/products beyond unsigned 64-bit byte capacity.
MAX_HOST_VALUE = (1 << 64) - 1
INSTALLED_ROOTS: tuple[tuple[ResourceKind, Path], ...] = (
    ("durable_state", Path("/var/lib/postcardscene")),
    ("cache", Path("/var/cache/postcardscene")),
    ("runtime", Path("/run/postcardscene")),
)


@dataclass(frozen=True)
class ResourceHealth:
    kind: ResourceKind
    available: bool
    capacity_bytes: int | None
    free_bytes: int | None
    state: HealthState
    reason: HealthReason


def evaluate_space(kind: ResourceKind, capacity: int, free: int) -> ResourceHealth:
    """Apply frozen V0 strict thresholds; invalid numeric inputs fail closed."""
    if (
        type(capacity) is not int
        or type(free) is not int
        or capacity <= 0
        or free < 0
        or free > capacity
    ):
        return ResourceHealth(
            kind, False, None, None, "unavailable", "invalid_statistics"
        )
    # Integer cross-products compare ratios exactly without floating-point overflow
    # or rounding at the strict 2% and 5% boundaries.
    state: HealthState = "healthy"
    if free < 256 * MIB or free * 100 < capacity * 2:
        state = "critical"
    elif free < GIB or free * 100 < capacity * 5:
        state = "warning"
    reason: HealthReason = "sufficient_space" if state == "healthy" else "low_space"
    return ResourceHealth(kind, True, capacity, free, state, reason)


def inspect_resource(kind: ResourceKind, root: Path) -> ResourceHealth:
    """Stat this exact caller-owned root only; never discover or mutate storage."""
    try:
        stats = os.statvfs(root)
        fragment = stats.f_frsize
        if type(fragment) is int and fragment == 0:
            fragment = stats.f_bsize
        counts = (fragment, stats.f_blocks, stats.f_bavail)
        if any(
            type(value) is not int or not 0 <= value <= MAX_HOST_VALUE
            for value in counts
        ):
            raise ValueError
        if fragment == 0:
            raise ValueError
        capacity = stats.f_blocks * fragment
        free = stats.f_bavail * fragment
        if capacity > MAX_HOST_VALUE or free > MAX_HOST_VALUE:
            raise ValueError
    except OSError:
        return ResourceHealth(kind, False, None, None, "unavailable", "stat_failed")
    except (AttributeError, TypeError, ValueError, ArithmeticError):
        return ResourceHealth(
            kind, False, None, None, "unavailable", "invalid_statistics"
        )
    return evaluate_space(kind, capacity, free)
