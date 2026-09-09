"""Closed host fixtures prove preflight outcomes without installing prerequisites."""

import importlib.util
import json
import stat
import sys
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "install_preflight", Path(__file__).resolve().parents[1] / "install_preflight.py"
)
pf = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pf
SPEC.loader.exec_module(pf)


class FixtureHost:
    def __init__(self, distro="ubuntu", version="24.04", code="noble"):
        self.files = {
            "/etc/passwd": "root:x:0:0:root:/root:/bin/bash\n",
            "/etc/group": "root:x:0:\n",
            "/etc/os-release": f'ID={distro}\nVERSION_ID="{version}"\nVERSION_CODENAME={code}\n',
            "/proc/sys/kernel/osrelease": "6.8.0-linux",
            "/proc/1/comm": "systemd\n",
            "/proc/self/mountinfo": "1 0 8:1 / / rw - ext4 /dev/root rw\n2 1 0:1 / /run rw - tmpfs tmpfs rw\n",
        }
        self.arch = "aarch64"
        self.width = 64
        self.python = ["CPython", [3, 12, 3], 64, "24.0"]
        self.present = {
            "/",
            "/usr",
            "/usr/bin",
            "/etc",
            "/etc/pam.d/common-session",
            "/usr/bin/python3",
        }
        self.installed = {"python3", "python3-venv"}
        self.overrides = {}
        self.stats = {}
        self.commands = []
        self.distro = distro
        self.codename = code

    def read(self, path):
        return self.files[path]

    def exists(self, path):
        return path in self.present

    def lstat(self, path):
        return self.stats.get(
            path, SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
        )

    def realpath(self, path):
        return {
            "/snap/bin/chromium": "/usr/bin/snap",
            "/usr/bin/python3": "/usr/bin/python3.12",
        }.get(path, path)

    def machine(self):
        return self.arch

    def bits(self):
        return self.width

    def command(self, args):
        self.commands.append(args)
        if args in self.overrides:
            return self.overrides[args]
        if args[0] == "/usr/bin/systemd-detect-virt":
            return 1, "none\n"
        if args == ("/usr/bin/dpkg", "--print-architecture"):
            return 0, "arm64\n"
        if args[:2] == ("/usr/bin/dpkg", "--verify"):
            return 0, ""
        if args[:2] == ("/usr/bin/dpkg-query", "-S"):
            package = {
                "/usr/bin/python3": "python3-minimal",
                "/usr/bin/python3.12": "python3.12-minimal",
                "/usr/bin/snap": "snapd",
            }.get(args[2], Path(args[2]).name)
            return 0, f"{package}: {args[2]}\n"
        if args[:2] == ("/usr/bin/dpkg-query", "-W"):
            if args[2] == "-f=${Version}":
                return 0, "135.0.0-1"
            return (0, "installed") if args[-1] in self.installed else (1, "")
        if args[0] == "/usr/bin/python3":
            assert args[1:3] == ("-I", "-B")
            return 0, json.dumps(self.python)
        if args[0] == "/usr/bin/apt-cache":
            domain = (
                "ports.ubuntu.com/ubuntu-ports"
                if self.distro == "ubuntu"
                else "deb.debian.org/debian"
            )
            return (
                0,
                f"{args[-1]}:\n  Installed: (none)\n  Candidate: 1.0.0\n  Version table:\n     1.0.0 500\n        500 http://{domain} {self.codename}/main arm64 Packages\n",
            )
        if args[0] == "/usr/bin/systemctl" and args[1] in (
            "list-unit-files",
            "list-units",
        ):
            return 0, ""
        if args[:2] == ("/usr/bin/systemctl", "show"):
            if args[2] == "systemd-logind.service":
                return 0, "LoadState=loaded\nActiveState=active\nUnitFileState=static\n"
            return 0, "LoadState=not-found\nActiveState=inactive\nUnitFileState=\n"
        if args[-1] == "--version":
            return 0, "tool 1.0.0\n"
        raise AssertionError(f"Unexpected command: {args}")


@pytest.mark.parametrize(
    "distro,version,code",
    [
        ("ubuntu", "24.04", "noble"),
        ("ubuntu", "26.04", "resolute"),
        ("debian", "13", "trixie"),
        ("raspbian", "13", "trixie"),
    ],
)
def test_supported_closed_immutable_plan(distro, version, code):
    host = FixtureHost(distro, version, code)
    result = pf.preflight(host)
    assert result.ok, result.reasons
    plan = result.plan
    assert plan.architecture == "aarch64"
    assert plan.apt_packages == pf.BASE_PACKAGES + (
        ("snapd",) if distro == "ubuntu" else ("chromium",)
    )
    assert plan.tools[-1].executable == (
        "/snap/bin/chromium" if distro == "ubuntu" else "/usr/bin/chromium"
    )
    assert plan.snap == (("chromium", "latest/stable") if distro == "ubuntu" else ())
    assert all(tool.state == "install_required" for tool in plan.tools)
    with pytest.raises(FrozenInstanceError):
        plan.distro = "injected"


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("arch", "x86_64", "unsupported_architecture"),
        ("width", 32, "unsupported_architecture"),
        ("python", ["CPython", [3, 10, 9], 64, "24.0"], "python_bootstrap_required"),
        ("python", ["CPython", [3, 15, 0], 64, "24.0"], "python_bootstrap_required"),
        ("python", ["PyPy", [3, 12, 0], 64, "24.0"], "python_bootstrap_required"),
    ],
)
def test_platform_and_python_fail_closed(field, value, reason):
    host = FixtureHost()
    setattr(host, field, value)
    assert pf.preflight(host) == pf.Result(False, (reason,))


@pytest.mark.parametrize(
    "path,value,reason",
    [
        (
            "/etc/os-release",
            "ID=evil\nID_LIKE=ubuntu\nVERSION_ID=24.04",
            "unsupported_os",
        ),
        (
            "/etc/os-release",
            "ID=ubuntu\nVERSION_ID=22.04\nVERSION_CODENAME=jammy",
            "unsupported_os",
        ),
        (
            "/proc/sys/kernel/osrelease",
            "microsoft-WSL2 secret",
            "unsupported_environment",
        ),
        ("/proc/1/comm", "other secret", "systemd_required"),
        (
            "/proc/self/mountinfo",
            "1 0 8:1 / / rw - nfs server:/secret rw\n",
            "unsupported_install_storage",
        ),
    ],
)
def test_bad_host_metadata_is_sanitized(path, value, reason):
    host = FixtureHost()
    host.files[path] = value
    assert pf.preflight(host) == pf.Result(False, (reason,))


def test_container_and_missing_venv():
    host = FixtureHost()
    host.overrides[("/usr/bin/systemd-detect-virt", "--container")] = (
        0,
        "docker secret",
    )
    assert pf.preflight(host).reasons == ("unsupported_environment",)
    host.overrides.clear()
    host.installed.remove("python3-venv")
    assert pf.preflight(host).reasons == ("python_bootstrap_required",)


@pytest.mark.parametrize(
    "mode,uid",
    [
        (stat.S_IFLNK | 0o777, 0),
        (stat.S_IFREG | 0o755, 0),
        (stat.S_IFDIR | 0o755, 1000),
        (stat.S_IFDIR | 0o777, 0),
    ],
)
def test_unsafe_existing_ancestors(mode, uid):
    host = FixtureHost()
    host.present.add("/var/lib")
    host.stats["/var/lib"] = SimpleNamespace(st_mode=mode, st_uid=uid)
    assert pf.preflight(host).reasons == ("unsafe_install_root",)


def test_unknown_installation_and_service_rejected():
    host = FixtureHost()
    host.present.add("/opt/postcardscene")
    assert pf.preflight(host).reasons == ("existing_installation_unrecognized",)
    host.present.remove("/opt/postcardscene")
    command = (
        "/usr/bin/systemctl",
        "show",
        pf.UNITS[0],
        "--no-pager",
        "--all",
        "--property=LoadState,ActiveState,UnitFileState",
    )
    host.overrides[command] = (
        0,
        "LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n",
    )
    assert pf.preflight(host).reasons == ("existing_service_unrecognized",)


def test_service_competition_is_explicit_action_and_seat_is_selected():
    host = FixtureHost()
    for unit in ("getty@tty1.service", "display-manager.service"):
        host.overrides[
            (
                "/usr/bin/systemctl",
                "show",
                unit,
                "--no-pager",
                "--all",
                "--property=LoadState,ActiveState,UnitFileState",
            )
        ] = (0, "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n")
    plan = pf.preflight(host, seat="seatd").plan
    assert plan.actions == (
        "reserve_tty1",
        "resolve_display_manager",
        "provision_seatd_socket_access",
    )
    assert plan.seat_packages == ("seatd",)
    assert pf.preflight(host, seat="secret; evil").reasons == (
        "unsupported_seat_authority",
    )


def test_installed_tool_validation_and_manual_conflict():
    host = FixtureHost()
    host.present.add("/usr/bin/labwc")
    assert pf.preflight(host).reasons == ("unsupported_tool_authority",)
    host.installed.add("labwc")
    assert pf.preflight(host).plan.tools[0].state == "installed"
    host.overrides[("/usr/bin/labwc", "--version")] = (0, "secret malformed")
    assert pf.preflight(host).reasons == ("tool_version_invalid",)
    host.overrides[("/usr/bin/labwc", "--version")] = (0, "labwc 0.6.0")
    assert pf.preflight(host).reasons == ("tool_version_unsupported",)
    host.present.remove("/usr/bin/labwc")
    assert pf.preflight(host).reasons == ("tool_unavailable",)


def test_package_origin_and_unavailability():
    host = FixtureHost()
    command = (
        "/usr/bin/apt-cache",
        "-o",
        "Dir::Cache::pkgcache=",
        "-o",
        "Dir::Cache::srcpkgcache=",
        "policy",
        "labwc",
    )
    host.overrides[command] = (0, "Candidate: (none)\n")
    assert pf.preflight(host).reasons == ("package_unavailable",)
    host.overrides[command] = (
        0,
        "Candidate: 1.0\n     1.0 500\n        500 https://secret.invalid/ noble/main arm64 Packages\n",
    )
    assert pf.preflight(host).reasons == ("unsupported_package_authority",)


def test_browser_authorities_do_not_launch_browser():
    host = FixtureHost("debian", "13", "trixie")
    host.present.add("/usr/bin/chromium")
    host.installed.add("chromium")
    assert pf.preflight(host).plan.tools[-1].state == "installed"
    assert not any(args[0] == "/usr/bin/chromium" for args in host.commands)
    host = FixtureHost()
    host.present.add("/usr/bin/chromium-browser")
    result = pf.preflight(host)
    assert result.ok
    assert result.plan.tools[-1].state == "install_required"


def test_preflight_mutation_sentinel(monkeypatch, tmp_path):
    # The entire successful orchestration runs with filesystem mutation forbidden.
    import builtins
    import os
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError("preflight attempted mutation or an unapproved subprocess")

    initial_files = tuple(tmp_path.iterdir())
    original_open = builtins.open

    def read_only(file, mode="r", *args, **kwargs):
        assert not any(flag in mode for flag in "wax+")
        return original_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", read_only)
    for name in (
        "mkdir",
        "makedirs",
        "chmod",
        "chown",
        "unlink",
        "remove",
        "rename",
        "replace",
        "rmdir",
    ):
        monkeypatch.setattr(os, name, forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    host = FixtureHost()
    before = dict(host.files)
    assert pf.preflight(host).ok
    assert host.files == before
    assert tuple(tmp_path.iterdir()) == initial_files
    assert "secret" not in json.dumps(asdict(pf.preflight(host)))


def test_missing_pam_logind_and_foreign_account():
    host = FixtureHost()
    host.present.remove("/etc/pam.d/common-session")
    assert pf.preflight(host).reasons == ("pam_required",)
    host.present.add("/etc/pam.d/common-session")
    host.files["/etc/passwd"] += "postcardscene:x:1000:1000:secret:/secret:/bin/sh\n"
    assert pf.preflight(host).reasons == ("existing_installation_unrecognized",)


def test_installed_snap_authority_and_sanitized_failure():
    host = FixtureHost()
    host.present.add("/snap/bin/chromium")
    command = ("/usr/bin/snap", "list", "chromium")
    host.overrides[command] = (
        0,
        "Name Version Rev Tracking Publisher Notes\nchromium 135.0.0 123 latest/stable canonical** -\n",
    )
    assert pf.preflight(host).plan.tools[-1].state == "installed"
    host.overrides[command] = (0, "secret invalid publisher\n")
    result = pf.preflight(host)
    assert result == pf.Result(False, ("unsupported_tool_authority",))
    assert "secret" not in json.dumps(asdict(result))


def test_real_read_only_command_boundary_ignores_injected_environment(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PYTHONPATH", "/secret")
    monkeypatch.setenv("HOME", str(tmp_path))
    status, output = pf.Host().command(
        (
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import os; print(os.getenv('PYTHONPATH')); print(os.getenv('HOME'))",
        )
    )
    assert status == 0
    assert output == "None\nNone\n"


def test_command_output_limit_and_fail_closed_read_error():
    with pytest.raises(pf.Rejected, match="command_unavailable"):
        pf.Host().command((sys.executable, "-I", "-B", "-c", "print('x'*300000)"))
    host = FixtureHost()
    del host.files["/proc/self/mountinfo"]
    assert pf.preflight(host) == pf.Result(False, ("host_inspection_unavailable",))


@pytest.mark.parametrize(
    "source",
    [
        "500 https://secret.invalid/ noble/main arm64 Packages",
        "500 http://ports.ubuntu.com/ubuntu-ports jammy/main arm64 Packages",
        "500 http://ports.ubuntu.com/ubuntu-ports noble/main amd64 Packages",
    ],
)
def test_mixed_or_wrong_release_package_authority_fails(source):
    host = FixtureHost()
    command = (
        "/usr/bin/apt-cache",
        "-o",
        "Dir::Cache::pkgcache=",
        "-o",
        "Dir::Cache::srcpkgcache=",
        "policy",
        "labwc",
    )
    _, output = host.command(command)
    host.overrides[command] = (0, output + "        " + source + "\n")
    assert pf.preflight(host).reasons == ("unsupported_package_authority",)


def test_read_only_mount_and_missing_installed_snap_launcher():
    host = FixtureHost()
    host.files["/proc/self/mountinfo"] = "1 0 8:1 / / ro - ext4 /dev/root rw\n"
    assert pf.preflight(host).reasons == ("unsupported_install_storage",)
    host = FixtureHost()
    host.present.add("/snap/chromium/current")
    assert pf.preflight(host).reasons == ("tool_unavailable",)


def test_internal_preserved_mode_retains_platform_policy_and_cli_default():
    host = FixtureHost()
    host.files["/etc/passwd"] += (
        "postcardscene:x:100:100::/var/lib/postcardscene:/usr/sbin/nologin\n"
    )
    assert pf.preflight(host).reasons == ("existing_installation_unrecognized",)
    result = pf.preflight(host, installation="preserved")
    assert result.ok and result.plan.installation == "preserved"
    host.arch = "x86_64"
    assert not pf.preflight(host, installation="preserved").ok
    assert not pf.preflight(FixtureHost(), installation="adopt").ok


def test_preserved_prerequisites_allow_only_inactive_not_found_cached_units():
    host = FixtureHost()
    listing = (
        "/usr/bin/systemctl",
        "list-units",
        "--all",
        "--no-legend",
        "--plain",
        "--no-pager",
        "postcardscene*",
    )
    host.overrides[listing] = (
        0,
        "postcardscene-runtime.service not-found inactive dead\n",
    )
    assert pf.preflight(host, installation="preserved").ok
    assert not pf.preflight(host).ok
    host.overrides[listing] = (
        0,
        "postcardscene-foreign.service not-found inactive dead\n",
    )
    assert not pf.preflight(host, installation="preserved").ok
