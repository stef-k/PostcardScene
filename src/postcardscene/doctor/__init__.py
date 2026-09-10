"""Closed, immutable administrative diagnostics; no repair authority."""

import json
import re
from dataclasses import asdict, dataclass

STATES = frozenset({"ready", "degraded", "unavailable", "fatal", "not_applicable"})
REASONS = frozenset(
    {
        "ready",
        "authority_invalid",
        "identity_mismatch",
        "query_failed",
        "deadline",
        "backup_disabled",
        "backup_status_unavailable",
        "backup_never",
        "backup_failed",
        "backup_due",
        "backup_retention_degraded",
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


SERVICE_VALUES = {
    "LoadState": frozenset({"loaded", "not-found", "error", "bad-setting", "masked"}),
    "UnitFileState": frozenset(
        {
            "enabled",
            "disabled",
            "static",
            "masked",
            "",
            "enabled-runtime",
            "linked",
            "linked-runtime",
            "masked-runtime",
            "indirect",
            "alias",
            "generated",
            "transient",
            "bad",
        }
    ),
    "ActiveState": frozenset(
        {
            "active",
            "inactive",
            "failed",
            "activating",
            "deactivating",
            "reloading",
            "maintenance",
            "refreshing",
        }
    ),
}


def valid_detail(identifier, key, value):
    if identifier == "release" and key == "version":
        return (
            type(value) is str
            and re.fullmatch(r"[0-9][a-z0-9.]{0,63}", value) is not None
        )
    if identifier.startswith("service_") and key in SERVICE_VALUES:
        return type(value) is str and value in SERVICE_VALUES[key]
    if identifier.startswith("storage_") and key == "space_state":
        return type(value) is str and value in {
            "healthy",
            "warning",
            "critical",
            "unavailable",
        }
    if identifier == "catalog" and key in {"sources", "items", "attention"}:
        return type(value) is int and 0 <= value < 2**63
    return False


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
        if type(self.details) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or not valid_detail(self.identifier, *item)
            for item in self.details
        ):
            raise ValueError("Invalid diagnostic details.")


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
