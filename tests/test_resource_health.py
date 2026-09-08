from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from postcardscene import resource_health as health


@pytest.mark.parametrize(
    "capacity,free,state",
    [
        (10 * health.GIB, 256 * health.MIB - 1, "critical"),
        (10 * health.GIB, 256 * health.MIB, "warning"),
        (10 * health.GIB, health.GIB - 1, "warning"),
        (10 * health.GIB, health.GIB, "healthy"),
        (100 * health.GIB, 2 * health.GIB - 1, "critical"),
        (100 * health.GIB, 2 * health.GIB, "warning"),
        (100 * health.GIB, 5 * health.GIB - 1, "warning"),
        (100 * health.GIB, 5 * health.GIB, "healthy"),
        (100 * health.GIB, 256 * health.MIB, "critical"),
        (100 * health.GIB, health.GIB, "critical"),
        (100 * health.MIB, 5 * health.MIB, "critical"),
        (10 * health.GIB, 0, "critical"),
    ],
)
def test_strict_policy_boundaries_and_precedence(capacity, free, state):
    result = health.evaluate_space("durable_state", capacity, free)
    assert result.state == state
    assert result.available
    assert (result.capacity_bytes, result.free_bytes) == (capacity, free)
    assert result.reason == ("sufficient_space" if state == "healthy" else "low_space")
    with pytest.raises(FrozenInstanceError):
        result.free_bytes = 0


@pytest.mark.parametrize(
    "capacity,free",
    [
        (0, 0),
        (-1, 0),
        (1, -1),
        (1, 2),
        (True, 0),
        (1, False),
        (None, 0),
        (1, None),
        ("10", 0),
        (10, "1"),
        (1.0, 0),
        (1, 0.0),
        (float("inf"), 0),
        (1, float("nan")),
    ],
)
def test_invalid_numeric_values_are_unavailable(capacity, free):
    result = health.evaluate_space("cache", capacity, free)
    assert result == health.ResourceHealth(
        "cache", False, None, None, "unavailable", "invalid_statistics"
    )


def stat_values(**overrides):
    return SimpleNamespace(
        **{
            "f_frsize": 4096,
            "f_bsize": 8192,
            "f_blocks": 10 * health.GIB // 4096,
            "f_bavail": health.GIB // 4096,
            "f_bfree": 9 * health.GIB // 4096,
            **overrides,
        }
    )


@pytest.mark.parametrize("fragment", [4096, 0])
def test_exact_root_fragment_and_unprivileged_blocks(monkeypatch, tmp_path, fragment):
    root = tmp_path / "owned"
    calls = []

    def stat(path):
        calls.append(path)
        return stat_values(f_frsize=fragment)

    monkeypatch.setattr(health.os, "statvfs", stat)
    result = health.inspect_resource("runtime", root)
    multiplier = 1 if fragment else 2
    assert result.capacity_bytes == 10 * health.GIB * multiplier
    assert result.free_bytes == health.GIB * multiplier
    assert calls == [root]


@pytest.mark.parametrize(
    "overrides",
    [
        {"f_frsize": -1},
        {"f_frsize": True},
        {"f_frsize": "4096"},
        {"f_frsize": 0, "f_bsize": 0},
        {"f_frsize": 0, "f_bsize": False},
        {"f_blocks": 0},
        {"f_blocks": -1},
        {"f_blocks": None},
        {"f_bavail": -1},
        {"f_bavail": True},
        {"f_bavail": float("inf")},
        {"f_bavail": 100 * health.GIB},
        {"f_blocks": 1 << 64},
        {"f_blocks": health.MAX_HOST_VALUE},
    ],
)
def test_invalid_host_statistics(monkeypatch, tmp_path, overrides):
    monkeypatch.setattr(health.os, "statvfs", lambda path: stat_values(**overrides))
    result = health.inspect_resource("cache", tmp_path)
    assert result.state == "unavailable"
    assert not result.available
    assert result.capacity_bytes is result.free_bytes is None


def test_stat_failure_is_sanitized_and_read_only(tmp_path):
    root = tmp_path / "missing-secret-root"
    before = tuple(tmp_path.iterdir())
    result = health.inspect_resource("durable_state", root)
    assert result.reason == "stat_failed"
    assert result.state == "unavailable"
    assert str(root) not in repr(result)
    assert tuple(tmp_path.iterdir()) == before
