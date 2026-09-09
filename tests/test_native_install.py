"""Installer gates, phase ordering and preservation of existing authority."""

import base64
import csv
import hashlib
import importlib.util
import io
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("native_install", ROOT / "install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


@pytest.fixture
def bundle(tmp_path):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    version = project["version"]
    info = f"postcardscene-{version}.dist-info"
    payload = {
        "postcardscene/" + name: (ROOT / "src/postcardscene" / name).read_bytes()
        for name in (
            *installer.ASSETS,
            "graphics/labwc/rc.xml",
            "graphics/labwc/autostart",
            "migrations/env.py",
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
    return tmp_path


def test_reviewed_inputs_accept_without_final_manifest(bundle):
    version, name, wheel, requirements, preflight = installer.validate_inputs(bundle)
    assert version in name
    assert wheel and requirements
    assert callable(preflight.preflight)
    assert not (bundle / "release-manifest.json").exists()


@pytest.mark.parametrize("name", tuple(installer.INPUT_HASHES))
def test_mismatched_support_rejected_without_execution(bundle, name):
    (bundle / name).write_text("raise AssertionError('untrusted support executed')")
    with pytest.raises(installer.InstallError, match="install_support_mismatch"):
        installer.validate_inputs(bundle)


def test_corrupt_wheel_record_rejected(bundle):
    wheel = next(bundle.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members["postcardscene/graphics/labwc/rc.xml"] = b"altered"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    with pytest.raises(installer.InstallError, match="invalid_wheel_record"):
        installer.validate_inputs(bundle)


def test_symlink_support_rejected(bundle):
    path = bundle / "install_preflight.py"
    path.unlink()
    path.symlink_to(ROOT / path.name)
    with pytest.raises(OSError):
        installer.validate_inputs(bundle)


def test_failed_preflight_has_no_mutation(bundle, monkeypatch):
    inputs = installer.validate_inputs(bundle)
    inputs[-1].preflight = lambda: SimpleNamespace(ok=False)
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
    inputs = installer.validate_inputs(bundle)
    plan = SimpleNamespace(tools=())
    inputs[-1].preflight = lambda: SimpleNamespace(ok=True, plan=plan)
    monkeypatch.setattr(installer, "validate_inputs", lambda _: inputs)
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    events = []

    def step(name):
        events.append(name)
        if failure == name:
            raise installer.InstallError("command_failed")

    monkeypatch.setattr(installer, "install_packages", lambda *a: step("packages"))
    monkeypatch.setattr(
        installer, "directory", lambda *a, **kw: events.append("directory")
    )
    monkeypatch.setattr(installer, "write_new", lambda *a: events.append("config"))
    monkeypatch.setattr(
        installer, "stage_payload", lambda *a: step("payload") or "/staged/bin/python"
    )
    monkeypatch.setattr(installer, "provision_identities", lambda *a: (11, 12, 13, 14))
    monkeypatch.setattr(installer, "install_assets", lambda *a: step("assets"))
    monkeypatch.setattr(installer, "reserve_graphics", lambda *a: step("conflicts"))
    monkeypatch.setattr(installer, "require_stopped", lambda *a: step("stopped"))
    monkeypatch.setattr(installer, "bootstrap", lambda *a: step("bootstrap"))
    monkeypatch.setattr(Path, "symlink_to", lambda *a: step("activation"))
    monkeypatch.setattr(
        installer.shutil,
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
    installer.bootstrap(
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
    monkeypatch.setattr(installer, "trusted_parent", lambda _: None)
    path = tmp_path / "config.py"
    path.write_bytes(b"existing authority")
    with pytest.raises(FileExistsError):
        installer.write_new(path, installer.CONFIG)
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
    with pytest.raises(installer.InstallError, match="service_state_unavailable"):
        if operation == "reserve_graphics":
            installer.reserve_graphics(preflight, lambda *a: pytest.fail("mutation"))
        else:
            installer.require_stopped(preflight)


@pytest.mark.parametrize("state", ["installed", "install_required"])
def test_closed_packages_preserve_already_installed_chromium(state):
    plan = SimpleNamespace(
        apt_packages=("labwc", "snapd"),
        seat_packages=(),
        snap=("chromium", "latest/stable"),
        tools=(SimpleNamespace(executable="/snap/bin/chromium", state=state),),
    )
    calls = []
    installer.install_packages(plan, lambda args, **kw: calls.append(args))
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
    monkeypatch.setattr(installer, "directory", lambda path: path.mkdir())
    monkeypatch.setattr(
        installer, "write_new", lambda path, data: path.write_bytes(data)
    )
    calls = []
    release = tmp_path / "0.1.0.dev0"
    result = installer.stage_payload(
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
    with pytest.raises(installer.InstallError, match="invalid_wheel_record"):
        installer.validate_inputs(bundle)
