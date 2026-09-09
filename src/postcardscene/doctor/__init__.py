"""Closed, immutable administrative diagnostics; no repair authority."""

import json
from dataclasses import asdict, dataclass

STATES = frozenset({"ready", "degraded", "unavailable", "fatal", "not_applicable"})
REASONS = frozenset(
    {
        "ready",
        "authority_invalid",
        "identity_mismatch",
        "query_failed",
        "deadline",
        "not_implemented",
        "not_running",
        "not_enabled",
        "service_failed",
        "not_installed",
        "sufficient_space",
        "low_space",
        "invalid_statistics",
        "stat_failed",
        "schema_incompatible",
        "integrity_failed",
        "database_unavailable",
        "sidecars_unavailable",
        "catalog_attention",
        "config_not_inspectable",
        "config_invalid",
        "direct_loopback",
        "direct_remote_opt_in",
        "same_host_https",
        "session_unavailable",
        "no_display",
        "output_unavailable",
        "tool_unavailable",
        "capability_unavailable",
        "software_ready",
    }
)
CHECKS = (
    "release",
    "config_authority",
    "installed_assets",
    "conflict_record",
    "identities",
    "durable_permissions",
    "database_permissions",
    "private_key_permissions",
    "cache_permissions",
    "runtime_permissions",
    "wayland_permissions",
    "service_graphics",
    "service_runtime",
    "service_web",
    "database",
    "catalog",
    "storage_durable_state",
    "storage_cache",
    "storage_runtime",
    "web_config",
    "graphics",
    "panel_cec",
    "panel_ddc",
    "panel_signal",
    "backup",
)


@dataclass(frozen=True)
class Check:
    identifier: str
    state: str
    reason: str
    # Only fixed enumerations or bounded numeric counts are public detail values.
    details: tuple[tuple[str, str | int], ...] = ()

    def __post_init__(self):
        if (
            self.identifier not in CHECKS
            or self.state not in STATES
            or self.reason not in REASONS
        ):
            raise ValueError("Invalid diagnostic vocabulary.")


@dataclass(frozen=True)
class Report:
    checks: tuple[Check, ...]

    @property
    def exit_code(self):
        if any(check.state == "fatal" for check in self.checks):
            return 2
        return int(
            any(check.state in {"degraded", "unavailable"} for check in self.checks)
        )

    def render(self, *, structured=False):
        if structured:
            return json.dumps(
                {
                    "exit_code": self.exit_code,
                    "checks": [asdict(c) for c in self.checks],
                },
                sort_keys=True,
            )
        return "\n".join(
            f"{c.identifier}: {c.state} ({c.reason})"
            + "".join(f"; {key}={value}" for key, value in c.details)
            for c in self.checks
        )
