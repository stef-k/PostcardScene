"""Trusted operator Python configuration shared by control and runtime."""

import os
from pathlib import Path

from postcardscene.persistence import DEFAULT_DATABASE_PATH


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
    return {
        "DATABASE_PATH": config.get("DATABASE_PATH", DEFAULT_DATABASE_PATH),
        "MEDIA_ALLOWED_ROOTS": config.get("MEDIA_ALLOWED_ROOTS", ()),
    }
