"""Non-executing schema compatibility and staged-environment PEP 440 contracts."""

import io
import json
import subprocess
import sys
import zipfile
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_install_preflight import FixtureHost, pf
from test_native_install import bundle as bundle
from test_native_install import installer


@pytest.fixture
def update(bundle):
    installer.load_support(bundle)
    return sys.modules["postcardscene_install_update"]


def graph_archive(revisions):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for index, (revision, parent) in enumerate(revisions):
            archive.writestr(
                f"postcardscene/migrations/versions/{index}.py",
                f"revision = {revision!r}\ndown_revision = {parent!r}\n"
                "raise AssertionError('migration must never execute')\n",
            )
    return zipfile.ZipFile(data)


@pytest.mark.parametrize(
    "revisions,head",
    [
        ([("a", None), ("b", "missing")], "b"),
        ([("a", None), ("b", "a"), ("c", "a")], "c"),
        ([("a", None), ("b", ("a", "c")), ("c", "a")], "b"),
        ([("a", "b"), ("b", "a")], "b"),
        ([("a", None), ("a", None)], "a"),
        ([("a", None), ("other", None)], "a"),
        ([("a", None)], "missing"),
    ],
)
def test_malformed_or_ambiguous_graph_refused(update, revisions, head):
    with graph_archive(revisions) as archive:
        with pytest.raises(update.inputs.InstallError, match="invalid_release_schema"):
            update.inputs.migration_graph(
                archive, {"application_id": 0x5053434E, "alembic_head": head}
            )


@pytest.mark.parametrize(
    "target,valid",
    [
        ({"a": None}, True),
        ({"a": None, "b": "a"}, True),
        ({"b": None}, False),
        ({"a": "x", "x": None}, False),
    ],
)
def test_schema_compatibility_uses_source_ancestry(update, target, valid):
    current = {"application_id": 0x5053434E, "schema": "a", "revisions": {"a": None}}
    newer = {**current, "revisions": target}
    if valid:
        update.inputs.require_forward_schema(current, newer)
    else:
        with pytest.raises(
            update.inputs.InstallError, match="incompatible_update_schema"
        ):
            update.inputs.require_forward_schema(current, newer)


@pytest.mark.parametrize(
    "current,target,valid",
    [
        ("1.0", "1.0.0", False),
        ("1.0rc1", "1.0", True),
        ("1.0", "1.0rc1", False),
        ("2.0", "1.0", False),
        ("1.0", "nonsense", False),
        ("bad", "2.0", False),
        ("1.0.dev1", "1.0a1", True),
        ("1.0", "1.0.post1", True),
    ],
)
def test_real_pep440_runs_in_selected_environment(update, current, target, valid):
    # This is the dependency-installed project venv, not the stdlib bootstrap.
    calls = []

    def run(args):
        calls.append(args)
        subprocess.run(args, check=True, capture_output=True, timeout=30)

    if valid:
        update.require_newer(sys.executable, current, target, run)
    else:
        with pytest.raises(subprocess.CalledProcessError):
            update.require_newer(sys.executable, current, target, run)
    assert calls[0][0] == sys.executable
    assert calls[0][1:4] == ("-I", "-B", "-c")


def test_state_parse_is_exact_and_bounded(update):
    state = update.state_files.UpdateState(
        "prepared", "1.0", "a" * 64, "a", "2.0", "b" * 64, "c" * 64, "b"
    )
    fields = update.state_files.asdict(state)
    assert update.state_files.parse(json.dumps(fields)) == state
    invalid = [
        {**fields, "recovery_archive": "/secret"},
        {**fields, "phase": "staging"},
        {**fields, "from_version": "../1"},
        {**fields, "to_version": "1.0"},
        {**fields, "from_schema": None},
        {**fields, "to_manifest_sha256": "X" * 64},
        [],
    ]
    for value in invalid:
        with pytest.raises(update.InstallError, match="invalid_update_state"):
            update.state_files.parse(json.dumps(value))
    for data in ('{"phase":"prepared","phase":"committed"}', " " * 4097, "{"):
        with pytest.raises(update.InstallError, match="invalid_update_state"):
            update.state_files.parse(data)


def test_exact_version_rejected_before_staging(update, monkeypatch):
    target = SimpleNamespace(version="1.0")
    monkeypatch.setattr(update, "active_version", lambda: "1.0")
    monkeypatch.setattr(update, "stored_release", lambda v: ({"version": v}, b"wheel"))
    with pytest.raises(update.InstallError, match="distinct_version"):
        update.recognize_current(target, None)


def test_managed_preflight_requires_installed_packages(monkeypatch):
    host = FixtureHost("debian", "13", "trixie")
    host.installed.update(pf.BASE_PACKAGES + ("chromium",))
    for package, executable in pf.TOOLS:
        host.present.add("/usr/bin/" + executable)
        host.overrides[("/usr/bin/dpkg-query", "-S", "/usr/bin/" + executable)] = (
            0,
            f"{package}: /usr/bin/{executable}\n",
        )
    host.present.add("/usr/bin/chromium")
    for unit in pf.UNITS:
        args = (
            "/usr/bin/systemctl",
            "show",
            unit,
            "--no-pager",
            "--all",
            "--property=LoadState,ActiveState,UnitFileState",
        )
        host.overrides[args] = (
            0,
            "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n",
        )
    result = pf.preflight(host, installation="installed_managed")
    assert result.ok, result.reasons
    host.installed.remove("mpv")
    assert pf.preflight(host, installation="installed_managed").reasons == (
        "update_prerequisites_missing",
    )
    assert not pf.preflight(host).ok
    host.installed.add("mpv")
    conflict = (
        "/usr/bin/systemctl",
        "show",
        "display-manager.service",
        "--no-pager",
        "--all",
        "--property=LoadState,ActiveState,UnitFileState",
    )
    host.overrides[conflict] = (
        0,
        "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n",
    )
    assert pf.preflight(host, installation="installed_managed").reasons == (
        "managed_graphics_conflict",
    )
    assert not any(
        c[0] in ("/usr/bin/apt-get", "/usr/sbin/useradd") for c in host.commands
    )


def test_target_manifest_and_wheel_identity_bound(update, bundle):
    _, _, _, members = installer.load_support(bundle)
    target = update.target_inputs(bundle, members)
    assert target.identity["version"] == target.version
    assert target.identity["schema"] in target.identity["revisions"]
    current = {**target.identity, "version": "0.0.1"}
    state = update.state_files.UpdateState(
        "prepared",
        "0.0.1",
        current["wheel_sha256"],
        current["schema"],
        target.version,
        target.identity["wheel_sha256"],
        target.manifest_sha256,
        target.identity["schema"],
    )
    update.require_state_target(state, current, target)
    with pytest.raises(update.InstallError, match="identity_mismatch"):
        update.require_state_target(
            replace(state, to_manifest_sha256="d" * 64), current, target
        )
