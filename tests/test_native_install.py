"""Installer gates, phase ordering and preservation of existing authority."""

import base64
import csv
import hashlib
import importlib.util
import io
import runpy
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("native_install", ROOT / "install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)
HOST_SPEC = importlib.util.spec_from_file_location(
    "test_install_host", ROOT / "install_host.py"
)
host = importlib.util.module_from_spec(HOST_SPEC)
HOST_SPEC.loader.exec_module(host)
release = SimpleNamespace(**runpy.run_path(str(ROOT / "scripts/release_bundle.py")))


@pytest.fixture
def bundle(tmp_path):
    tmp_path = tmp_path / "bundle"
    tmp_path.mkdir()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    version = project["version"]
    info = f"postcardscene-{version}.dist-info"
    payload = {
        "postcardscene/" + name: (ROOT / "src/postcardscene" / name).read_bytes()
        for name in (
            *host.ASSETS,
            "graphics/labwc/rc.xml",
            "graphics/labwc/autostart",
            "migrations/env.py",
            "persistence.py",
            *(
                str(p.relative_to(ROOT / "src/postcardscene"))
                for p in (ROOT / "src/postcardscene/migrations/versions").glob("*.py")
            ),
        )
    }
    payload.update(
        {
            f"{info}/METADATA": f"Name: postcardscene\nVersion: {version}\nRequires-Python: >=3.11,<3.15\n".encode(),
            f"{info}/WHEEL": b"Root-Is-Purelib: true\nTag: py3-none-any\n",
            f"{info}/entry_points.txt": (
                "[console_scripts]\n"
                + "\n".join(f"{k} = {v}" for k, v in project["scripts"].items())
            ).encode(),
        }
    )
    record = io.StringIO()
    writer = csv.writer(record)
    for name, data in payload.items():
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        )
        writer.writerow((name, "sha256=" + digest, len(data)))
    writer.writerow((f"{info}/RECORD", "", ""))
    payload[f"{info}/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(
        tmp_path / f"postcardscene-{version}-py3-none-any.whl", "w"
    ) as wheel:
        wheel.writestr("postcardscene/", b"")
        for name, data in payload.items():
            wheel.writestr(name, data)
    for name in installer.INPUT_HASHES:
        (tmp_path / name).write_bytes((ROOT / name).read_bytes())
    (tmp_path / "install.py").write_bytes((ROOT / "install.py").read_bytes())
    release.write_manifest(tmp_path, version, "a" * 40)
    return tmp_path


def test_reviewed_inputs_accept_with_final_manifest(bundle):
    version, name, wheel, requirements, preflight, loaded_host = (
        installer.validate_inputs(bundle)
    )
    assert version in name
    assert wheel and requirements
    assert callable(preflight.preflight)
    assert callable(loaded_host.stage_payload)
    assert (bundle / "release-manifest.json").exists()


@pytest.mark.parametrize("name", tuple(installer.INPUT_HASHES))
def test_mismatched_support_rejected_without_execution(bundle, monkeypatch, name):
    (bundle / name).write_text("raise AssertionError('untrusted support executed')")
    monkeypatch.setattr(
        installer.importlib.util,
        "module_from_spec",
        lambda _: pytest.fail("support executed before input authentication"),
    )
    operation = installer.Installation(bundle, lambda *a, **kw: pytest.fail("mutation"))
    with pytest.raises(installer.InstallError, match="install_support_mismatch"):
        operation.install()
    assert operation.release is None


def test_corrupt_wheel_record_rejected(bundle):
    wheel = next(bundle.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members["postcardscene/graphics/labwc/rc.xml"] = b"altered"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    release.write_manifest(bundle, "0.1.0.dev0", "a" * 40)
    with pytest.raises(installer.InstallError, match="invalid_wheel_record"):
        installer.validate_inputs(bundle)


@pytest.mark.parametrize("name", tuple(installer.INPUT_HASHES))
def test_symlink_support_rejected(bundle, name):
    path = bundle / name
    path.unlink()
    path.symlink_to(ROOT / path.name)
    with pytest.raises(OSError):
        installer.validate_inputs(bundle)


def test_failed_preflight_has_no_mutation(bundle, monkeypatch):
    inputs = (*installer.validate_inputs(bundle)[:-1], host)
    monkeypatch.setattr(host, "classify", lambda *a: "clean")
    inputs[-2].preflight = lambda: SimpleNamespace(ok=False)
    monkeypatch.setattr(installer, "validate_inputs", lambda _: inputs)
    calls = []
    operation = installer.Installation(bundle, lambda *a, **kw: calls.append(a))
    with pytest.raises(installer.InstallError, match="preflight_rejected"):
        operation.install()
    assert calls == []
    assert operation.phase == "validation"
    assert operation.release is None


@pytest.mark.parametrize("failure", ["packages", "bootstrap", "activation", None])
def test_ordering_and_failure_preservation(bundle, monkeypatch, failure, capsys):
    inputs = (*installer.validate_inputs(bundle)[:-1], host)
    plan = SimpleNamespace(tools=())
    monkeypatch.setattr(host, "classify", lambda *a: "clean")
    inputs[-2].preflight = lambda: SimpleNamespace(ok=True, plan=plan)
    monkeypatch.setattr(installer, "validate_inputs", lambda _: inputs)
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    events = []

    def step(name):
        events.append(name)
        if failure == name:
            raise host.InstallError("command_failed")

    monkeypatch.setattr(host, "install_packages", lambda *a: step("packages"))
    monkeypatch.setattr(host, "directory", lambda *a, **kw: events.append("directory"))
    monkeypatch.setattr(host, "write_new", lambda *a: events.append("config"))
    monkeypatch.setattr(
        host, "stage_payload", lambda *a: step("payload") or "/staged/bin/python"
    )
    monkeypatch.setattr(host, "provision_identities", lambda *a: (11, 12, 13, 14))
    monkeypatch.setattr(host, "install_assets", lambda *a: step("assets"))
    monkeypatch.setattr(host, "reserve_graphics", lambda *a: step("conflicts"))
    monkeypatch.setattr(host, "require_stopped", lambda *a: step("stopped"))
    monkeypatch.setattr(host, "bootstrap", lambda *a: step("bootstrap"))
    monkeypatch.setattr(Path, "symlink_to", lambda *a: step("activation"))
    monkeypatch.setattr(
        host.shutil,
        "rmtree",
        lambda *a: pytest.fail("durable failure deleted payload"),
    )
    operation = installer.Installation(bundle, lambda args: events.append(args[1]))
    if failure:
        with pytest.raises(installer.InstallError):
            operation.install()
        operation.recover()
        assert "Install failed during" in capsys.readouterr().err
        assert "start" not in events
        if failure == "packages":
            assert events == ["packages"]
        else:
            assert events[-1] == "stop"
    else:
        operation.install()
        assert (
            events.index("payload") < events.index("config") < events.index("bootstrap")
        )
        assert (
            events.index("stopped")
            < events.index("bootstrap")
            < events.index("activation")
        )
        assert (
            events.index("activation")
            < events.index("daemon-reload")
            < events.index("enable")
            < events.index("start")
        )
        assert operation.phase == "complete"


def test_bootstrap_uses_web_cli_without_password_arguments():
    calls = []
    host.bootstrap(
        "/release/venv/bin/python", lambda args, **kw: calls.append((args, kw))
    )
    assert calls[0][1]["user"] == "postcardscene"
    assert "O_EXCL" in calls[0][0][-1] and "0o660" in calls[0][0][-1]
    assert [args[-2:] for args, _ in calls[1:]] == [
        ("auth", "init-secret"),
        ("db", "upgrade"),
        ("db", "check"),
        ("auth", "create-admin"),
    ]
    assert all(options["user"] == "postcardscene-web" for _, options in calls[1:])
    assert calls[-1][1]["interactive"] is True
    assert all("password" not in arg for args, _ in calls for arg in args)


def test_exclusive_config_write_never_replaces_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "trusted_parent", lambda _: None)
    path = tmp_path / "config.py"
    path.write_bytes(b"existing authority")
    with pytest.raises(FileExistsError):
        host.write_new(path, host.CONFIG)
    assert path.read_bytes() == b"existing authority"


@pytest.mark.parametrize("operation", ["require_stopped", "reserve_graphics"])
def test_unavailable_service_state_uses_installer_recovery(operation):
    class Rejected(Exception):
        pass

    def unavailable(*args):
        raise Rejected("service_state_unavailable")

    preflight = SimpleNamespace(
        Rejected=Rejected, Host=lambda: None, service_state=unavailable
    )
    with pytest.raises(host.InstallError, match="service_state_unavailable"):
        if operation == "reserve_graphics":
            host.reserve_graphics(preflight, lambda *a: pytest.fail("mutation"))
        else:
            host.require_stopped(preflight)


@pytest.mark.parametrize("state", ["installed", "install_required"])
def test_closed_packages_preserve_already_installed_chromium(state):
    plan = SimpleNamespace(
        apt_packages=("labwc", "snapd"),
        seat_packages=(),
        snap=("chromium", "latest/stable"),
        tools=(SimpleNamespace(executable="/snap/bin/chromium", state=state),),
    )
    calls = []
    host.install_packages(plan, lambda args, **kw: calls.append(args))
    assert calls[0][-1] == "update"
    assert calls[1][-3:] == ("install", "labwc", "snapd")
    assert (len(calls) == 3) == (state == "install_required")
    if state == "install_required":
        assert calls[-1] == (
            "/usr/bin/snap",
            "install",
            "chromium",
            "--channel=latest/stable",
        )


def test_staging_enforces_isolated_hash_locked_binary_install(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "directory", lambda path: path.mkdir())
    monkeypatch.setattr(host, "write_new", lambda path, data: path.write_bytes(data))
    calls = []
    release = tmp_path / "0.1.0.dev0"
    result = host.stage_payload(
        release,
        "postcardscene-0.1.0.dev0-py3-none-any.whl",
        b"wheel",
        b"requirements",
        SimpleNamespace(python="/usr/bin/python3"),
        lambda args, **kw: calls.append(args),
    )
    assert calls[0] == ("/usr/bin/python3", "-I", "-m", "venv", str(release / "venv"))
    assert result == str(release / "venv/bin/python")
    requirements, app = calls[1:3]
    assert requirements[0] == app[0] == result
    assert all(
        option in requirements
        for option in ("--isolated", "--require-hashes", "--only-binary=:all:")
    )
    assert "--no-deps" in app
    assert "check" in calls[3]


def test_blank_wheel_record_row_rejected_safely(bundle):
    path = next(bundle.glob("*.whl"))
    with zipfile.ZipFile(path) as wheel:
        members = {name: wheel.read(name) for name in wheel.namelist()}
    record = next(name for name in members if name.endswith("/RECORD"))
    members[record] = b"\n" + members[record]
    with zipfile.ZipFile(path, "w") as wheel:
        for name, content in members.items():
            wheel.writestr(name, content)
    release.write_manifest(bundle, "0.1.0.dev0", "a" * 40)
    with pytest.raises(installer.InstallError, match="invalid_wheel_record"):
        installer.validate_inputs(bundle)


def test_doctor_does_not_open_wheel_entry_point_allowlist(bundle):
    wheel = next(bundle.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    entry_path = next(name for name in members if name.endswith("/entry_points.txt"))
    assert (
        b"postcardscene-doctor = postcardscene.doctor.cli:main" in members[entry_path]
    )
    members[entry_path] += b"\nunreviewed-tool = arbitrary.module:main\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    release.write_manifest(bundle, "0.1.0.dev0", "a" * 40)
    with pytest.raises(installer.InstallError, match="invalid_wheel_entry_points"):
        installer.validate_inputs(bundle)


@pytest.mark.parametrize("name", tuple(installer.INPUT_HASHES))
@pytest.mark.parametrize("kind", ("hardlink", "fifo", "directory", "oversized"))
def test_invalid_support_never_executes_or_mutates(bundle, monkeypatch, name, kind):
    path = bundle / name
    path.unlink()
    if kind == "hardlink":
        original = bundle / "linked-input"
        original.write_bytes((ROOT / name).read_bytes())
        path.hardlink_to(original)
    elif kind == "fifo":
        installer.os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        with path.open("wb") as stream:
            stream.truncate(32 * 1024 * 1024 + 1)
    monkeypatch.setattr(
        installer.importlib.util,
        "module_from_spec",
        lambda _: pytest.fail("support executed before input authentication"),
    )
    operation = installer.Installation(
        bundle, lambda *a, **kw: pytest.fail("host mutation")
    )
    with pytest.raises((installer.InstallError, OSError)):
        operation.install()
    assert operation.phase == "validation"
    assert operation.release is None


@pytest.mark.parametrize("state", ["clean", "installed_managed", "partial_or_unknown"])
def test_reinstall_cannot_request_preserved_preflight_before_recognition(
    bundle, monkeypatch, state
):
    inputs = (*installer.validate_inputs(bundle)[:-1], host)
    monkeypatch.setattr(installer, "validate_inputs", lambda _: inputs)
    monkeypatch.setattr(host, "classify", lambda *a: state)

    def preflight(**options):
        assert options.get("installation", "clean") == "clean"
        return SimpleNamespace(ok=False)

    inputs[-2].preflight = preflight
    with pytest.raises(installer.InstallError):
        installer.Installation(bundle, lambda *a: pytest.fail("mutation")).install()


def test_removed_remove_is_idempotent_without_restoration(bundle, monkeypatch):
    inputs = (*installer.validate_inputs(bundle)[:-1], host)
    monkeypatch.setattr(installer, "validate_inputs", lambda _: inputs)
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(host, "classify", lambda *a: "removed_preserved")
    monkeypatch.setattr(
        host, "remove_managed", lambda *a: pytest.fail("destructive replay")
    )
    operation = installer.Installation(bundle, lambda *a: pytest.fail("service replay"))
    operation.remove()
    assert operation.phase == "already_removed_preserved"


def test_reinstall_incompatible_database_stops_before_reprovisioning(
    bundle, monkeypatch
):
    inputs = (*installer.validate_inputs(bundle)[:-1], host)
    monkeypatch.setattr(installer, "validate_inputs", lambda _: inputs)
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(host, "classify", lambda *a: "removed_preserved")
    modes = []

    def preflight(**options):
        modes.append(options["installation"])
        return SimpleNamespace(ok=True, plan=SimpleNamespace(tools=()))

    inputs[-2].preflight = preflight
    monkeypatch.setattr(host, "install_packages", lambda *a: None)
    monkeypatch.setattr(host, "directory", lambda *a: None)
    monkeypatch.setattr(host, "stage_payload", lambda *a: "/staged/bin/python")

    def incompatible(*args):
        raise host.InstallError("command_failed")

    monkeypatch.setattr(installer, "validate_preserved_application", incompatible)
    for name in ("install_assets", "reserve_graphics", "activate_payload"):
        monkeypatch.setattr(
            host, name, lambda *a: pytest.fail("mutated preserved authority")
        )
    operation = installer.Installation(bundle, lambda *a: pytest.fail("mutation"))
    with pytest.raises(installer.InstallError):
        operation.install()
    assert modes == ["preserved", "preserved"]
    assert operation.phase == "preserved_compatibility"
    assert not operation.activation and not operation.durable


def test_preserved_application_checks_only_detached_database_and_existing_key():
    calls = []
    installer.validate_preserved_application(
        "/staged/python", lambda args, **kw: calls.append((args, kw))
    )
    assert len(calls) == 2
    assert "snapshot(Path(scratch))" in calls[0][0][-1]
    assert "Administrator" in calls[0][0][-1]
    assert "read_secret" in calls[1][0][-1]
    assert calls[1][1]["user"] == "postcardscene-web"
    assert all(
        "upgrade" not in str(call) and "initialize_secret" not in str(call)
        for call in calls
    )


@pytest.mark.parametrize("failure", ["stop", "process", "restore"])
def test_remove_failure_preserves_payload_and_uncertain_targets(monkeypatch, failure):
    events = []
    monkeypatch.setattr(host, "installed_authority", lambda *a: None)
    monkeypatch.setattr(host, "service_authority", lambda *a: None)
    monkeypatch.setattr(host, "conflict_record", lambda *a: {})
    monkeypatch.setattr(host, "preserved_authority", lambda: (11, 12, 13, 14))
    monkeypatch.setattr(host, "require_stopped", lambda *a: None)

    def step(name):
        events.append(name)
        if failure == name:
            raise host.InstallError("command_failed")

    monkeypatch.setattr(host, "require_no_processes", lambda *a: step("process"))
    monkeypatch.setattr(host, "restore_graphics", lambda *a: step("restore"))
    monkeypatch.setattr(
        Path, "unlink", lambda *a: pytest.fail("deleted uncertain authority")
    )
    with pytest.raises(host.InstallError):
        host.remove_managed("0.1.0.dev0", b"", None, lambda args: step(args[1]))
    assert events[-1] == failure
    assert all(name in {"stop", "disable", "process", "restore"} for name in events)


def test_nested_same_device_mount_is_not_a_removable_tree(tmp_path, monkeypatch):
    nested = tmp_path / "nested"
    nested.mkdir()
    data = nested / "external-data"
    data.write_bytes(b"keep")
    monkeypatch.setattr(
        host,
        "read_regular",
        lambda *a: f"1 0 8:1 /elsewhere {nested} rw - ext4 /dev/root rw\n".encode(),
    )
    with pytest.raises(host.InstallError, match="unsafe_removal_tree"):
        host.validate_tree(tmp_path, tmp_path.stat().st_uid, tmp_path.stat().st_gid)
    assert data.read_bytes() == b"keep"


@pytest.mark.parametrize("extra", ["dropin", "unit"])
def test_foreign_effective_service_authority_rejected(extra):
    observer = SimpleNamespace(
        command=lambda args: (
            0,
            f"FragmentPath=/etc/systemd/system/{args[2]}\nDropInPaths=/run/systemd/system/foreign.conf\n",
        )
    )
    preflight = SimpleNamespace(
        Host=lambda: observer,
        unit_names=lambda _: (
            set(host.SERVICES)
            | ({"postcardscene-extra.service"} if extra == "unit" else set())
        ),
    )
    with pytest.raises(host.InstallError, match="managed_service_authority_invalid"):
        host.service_authority(preflight)


def test_process_quiescence_waits_boundedly_without_killing_foreign_processes(
    monkeypatch,
):
    states = iter((True, False))
    monkeypatch.setattr(host, "owned_processes", lambda _: next(states))
    monkeypatch.setattr(host.time, "sleep", lambda _: None)
    host.require_no_processes((11, 12, 13, 14))
    monkeypatch.setattr(host, "owned_processes", lambda _: True)
    times = iter((0, 16))
    monkeypatch.setattr(host.time, "monotonic", lambda: next(times))
    with pytest.raises(host.InstallError, match="owned_processes_remain"):
        host.require_no_processes((11, 12, 13, 14))


def test_process_inspection_checks_all_uid_authority_fields(tmp_path, monkeypatch):
    process = tmp_path / "123"
    process.mkdir()
    status = process / "status"
    monkeypatch.setattr(host, "Path", lambda _: tmp_path)
    status.write_text("Name:\ttest\nUid:\t0\t11\t0\t0\n")
    assert host.owned_processes((11, 12, 13, 14))
    status.write_text("Name:\ttest\nUid:\t0\t0\t0\t0\n")
    assert not host.owned_processes((11, 12, 13, 14))


def test_restore_active_masked_unit_starts_before_restoring_mask(tmp_path, monkeypatch):
    state = {
        "LoadState": "masked",
        "ActiveState": "active",
        "UnitFileState": "masked",
        "local_symlink": "/dev/null",
    }
    monkeypatch.setattr(host, "Path", lambda _: tmp_path)
    monkeypatch.setattr(
        host,
        "service_state",
        lambda *a: {k: v for k, v in state.items() if k != "local_symlink"},
    )
    alias = tmp_path / "getty@tty1.service"
    calls = []

    def run(args):
        calls.append(args[1])
        if args[1] == "start":
            assert not alias.is_symlink()

    host.restore_graphics(None, run, {"getty@tty1.service": state})
    assert alias.readlink() == Path("/dev/null")
    assert calls == ["daemon-reload", "start", "daemon-reload"]
