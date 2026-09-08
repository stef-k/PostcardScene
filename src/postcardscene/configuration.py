"""Trusted operator Python configuration shared by control and runtime."""

import os
from pathlib import Path

from postcardscene.graphics.output_probe import CONNECTOR
from postcardscene.persistence import DEFAULT_DATABASE_PATH
from postcardscene.power.cec import DEVICE


def load_operator_config():
    """Execute the explicitly selected host file; missing/unreadable files fail."""
    if "POSTCARDSCENE_CONFIG" not in os.environ:
        return {}
    path = Path(os.environ["POSTCARDSCENE_CONFIG"])
    namespace = {"__file__": str(path)}
    exec(compile(path.read_bytes(), str(path), "exec"), namespace)
    return {key: value for key, value in namespace.items() if key.isupper()}


def load_runtime_config():
    config = load_operator_config()
    enabled = config.get("PANEL_POWER_RUNTIME_ENABLED", False)
    cec = config.get("CEC_DEVICE")
    ddc = config.get("DDC_DISPLAY")
    connector = config.get("DISPLAY_CONNECTOR")
    if type(enabled) is not bool:
        raise ValueError("Panel runtime enablement must be boolean.")
    if cec is not None and (type(cec) is not str or not DEVICE.fullmatch(cec)):
        raise ValueError("Invalid trusted CEC selector.")
    if ddc is not None and (type(ddc) is not int or not 1 <= ddc <= 9999):
        raise ValueError("Invalid trusted DDC selector.")
    if connector is not None and (
        type(connector) is not str or not CONNECTOR.fullmatch(connector)
    ):
        raise ValueError("Invalid trusted display connector.")
    if not enabled and any(value is not None for value in (cec, ddc, connector)):
        raise ValueError("Panel selectors require explicit runtime enablement.")
    return {
        "PANEL_POWER_RUNTIME_ENABLED": enabled,
        "CEC_DEVICE": cec,
        "DDC_DISPLAY": ddc,
        "DISPLAY_CONNECTOR": connector,
        "DATABASE_PATH": config.get("DATABASE_PATH", DEFAULT_DATABASE_PATH),
        "MEDIA_ALLOWED_ROOTS": config.get("MEDIA_ALLOWED_ROOTS", ()),
    }
