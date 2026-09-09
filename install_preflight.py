"""Read-only native install support; runnable without installing PostcardScene."""

import argparse
import json
import os
import platform
import re
import selectors
import signal
import stat
import struct
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

ROOTS = (
    "/opt/postcardscene",
    "/etc/postcardscene",
    "/var/lib/postcardscene",
    "/var/lib/postcardscene-web",
    "/var/cache/postcardscene",
    "/run/postcardscene",
    "/run/postcardscene-wayland",
)
TOOLS = (
    ("labwc", "labwc"),
    ("wlr-randr", "wlr-randr"),
    ("wlopm", "wlopm"),
    ("mpv", "mpv"),
    ("ddcutil", "ddcutil"),
    ("v4l-utils", "cec-ctl"),
)
BASE_PACKAGES = tuple(p for p, _ in TOOLS) + (
    "python3",
    "python3-venv",
    "systemd",
    "systemd-sysv",
    "libpam-systemd",
    "libseat1",
)
UNITS = (
    "postcardscene-runtime.service",
    "postcardscene-web.service",
    "postcardscene-graphics.service",
)
LOCAL_FILESYSTEMS = frozenset({"ext4", "ext3", "ext2", "btrfs", "xfs", "f2fs", "zfs"})
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C"}


class Rejected(Exception):
    """Only code-owned reason strings may cross the diagnostic boundary."""


@dataclass(frozen=True)
class Tool:
    package: str
    executable: str
    state: str


@dataclass(frozen=True)
class Plan:
    distro: str
    release: str
    architecture: str
    systemd: bool
    python: str
    python_version: str
    authority: str
    apt_packages: tuple[str, ...]
    seat_packages: tuple[str, ...]
    seat: str
    snap: tuple[str, ...]
    tools: tuple[Tool, ...]
    actions: tuple[str, ...]
    installation: str = "clean"


@dataclass(frozen=True)
class Result:
    ok: bool
    reasons: tuple[str, ...]
    plan: Plan | None = None


class Host:
    """Bounded local reads and read-only commands; no application dependencies."""

    def read(self, path):
        with open(path, "rb") as stream:
            data = stream.read(262145)
        if len(data) > 262144:
            raise Rejected("host_metadata_unavailable")
        return data.decode("utf-8", errors="strict")

    def exists(self, path):
        return os.path.lexists(path)

    def lstat(self, path):
        return os.lstat(path)

    def realpath(self, path):
        return os.path.realpath(path)

    def machine(self):
        return platform.machine()

    def bits(self):
        return struct.calcsize("P") * 8

    def command(self, args):
        # No inherited Python, loader, proxy, user-home or service environment.
        with subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=ENV,
            start_new_session=True,
        ) as process:
            try:
                return self._capture(process)
            finally:
                # Also retire descendants of wrappers on timeout/output overflow.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=1)

    def _capture(self, process):
        data = bytearray()
        deadline = time.monotonic() + 5
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not selector.select(timeout=0.05):
                    continue
                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk:
                    return process.wait(timeout=1), data.decode("utf-8", "strict")
                data.extend(chunk)
                if len(data) > 262144:
                    break
        raise Rejected("command_unavailable")


def identity(host):
    values = {}
    for line in host.read("/etc/os-release").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"').strip("'")
    distro, version = values.get("ID"), values.get("VERSION_ID", "")
    code = values.get("VERSION_CODENAME")
    if distro == "ubuntu" and (version, code) in (
        ("24.04", "noble"),
        ("26.04", "resolute"),
    ):
        return "ubuntu", version
    if distro in ("debian", "raspbian") and version == "13" and code == "trixie":
        return "debian-trixie", "13"
    raise Rejected("unsupported_os")


def platform_check(host):
    if host.machine() not in ("aarch64", "arm64") or host.bits() != 64:
        raise Rejected("unsupported_architecture")
    if "microsoft" in host.read("/proc/sys/kernel/osrelease").lower():
        raise Rejected("unsupported_environment")
    status, output = host.command(("/usr/bin/systemd-detect-virt", "--container"))
    if status != 1 or output.strip() != "none":
        raise Rejected("unsupported_environment")
    if host.read("/proc/1/comm").strip() != "systemd":
        raise Rejected("systemd_required")
    status, output = host.command(("/usr/bin/dpkg", "--print-architecture"))
    if status or output.strip() != "arm64":
        raise Rejected("unsupported_architecture")


def package_status(host, package):
    status, output = host.command(
        (
            "/usr/bin/dpkg-query",
            "-W",
            "-f=${db:Status-Status}",
            package,
        )
    )
    if status == 1:
        return False
    if status or output.strip() not in ("installed", "not-installed", "config-files"):
        raise Rejected("package_state_invalid")
    return output.strip() == "installed"


def owned(host, package, path):
    status, output = host.command(("/usr/bin/dpkg-query", "-S", path))
    if status or not any(
        line in (f"{package}: {path}", f"{package}:arm64: {path}")
        for line in output.splitlines()
    ):
        raise Rejected("unsupported_tool_authority")
    resolved = host.realpath(path)
    if not resolved.startswith("/usr/"):
        raise Rejected("unsupported_tool_authority")
    for ancestor in (Path(resolved), *Path(resolved).parents):
        info = host.lstat(str(ancestor))
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise Rejected("unsupported_tool_authority")
    status, output = host.command(("/usr/bin/dpkg", "--verify", package))
    if status or output.strip():
        raise Rejected("unsupported_tool_authority")


def package_available(host, package, distro):
    status, output = host.command(
        (
            "/usr/bin/apt-cache",
            "-o",
            "Dir::Cache::pkgcache=",
            "-o",
            "Dir::Cache::srcpkgcache=",
            "policy",
            package,
        )
    )
    if status or not re.search(r"Candidate: (?!\(none\))\S+", output):
        raise Rejected("package_unavailable")
    # Require the candidate's origin, not any unrelated version in the cache.
    candidate = re.search(r"Candidate: (\S+)", output).group(1)
    section = re.search(
        r"^\s*(?:\*\*\* )?" + re.escape(candidate) + r" \d+\n((?:\s{8,}.*\n?)*)",
        output,
        re.MULTILINE,
    )
    if not section:
        raise Rejected("unsupported_package_authority")
    domains = (
        r"(?:ports|archive|security)\.ubuntu\.com"
        if distro == "ubuntu"
        else r"(?:deb|security)\.debian\.org|archive\.raspberrypi\.com"
    )
    if not re.search(r"https?://(?:" + domains + r")/", section.group(1)):
        raise Rejected("unsupported_package_authority")


def python_check(host):
    if not package_status(host, "python3") or not package_status(host, "python3-venv"):
        raise Rejected("python_bootstrap_required")
    owned(host, "python3-minimal", "/usr/bin/python3")
    script = (
        "import sys,struct,platform,venv,ensurepip,json; "
        "print(json.dumps([platform.python_implementation(),"
        "list(sys.version_info[:3]),struct.calcsize('P')*8,ensurepip.version()]))"
    )
    status, output = host.command(("/usr/bin/python3", "-I", "-B", "-c", script))
    try:
        implementation, version, bits, pip = json.loads(output)
        valid = (
            implementation == "CPython"
            and bits == 64
            and len(version) == 3
            and all(type(n) is int for n in version)
            and (3, 11) <= tuple(version[:2]) < (3, 15)
            and re.fullmatch(r"\d+(?:\.\d+)+", pip)
        )
    except (ValueError, TypeError):
        valid = False
    if status or not valid:
        raise Rejected("python_bootstrap_required")
    return ".".join(map(str, version))


def storage_check(host):
    mounts = []
    for line in host.read("/proc/self/mountinfo").splitlines():
        fields = line.split()
        separator = fields.index("-")
        path = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])
        mounts.append((path, fields[separator + 1]))
    for root in ROOTS:
        path = Path(root)
        for ancestor in reversed((path, *path.parents)):
            if not host.exists(str(ancestor)):
                continue
            info = host.lstat(str(ancestor))
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0
                or info.st_mode & 0o022
            ):
                raise Rejected("unsafe_install_root")
        if host.exists(root):
            # No managed marker exists before #140/#142: never adopt even empty roots.
            raise Rejected("existing_installation_unrecognized")
        filesystem = max(
            (
                (p, fs)
                for p, fs in mounts
                if root == p or root.startswith(p.rstrip("/") + "/")
            ),
            key=lambda item: len(item[0]),
            default=("", "unknown"),
        )[1]
        allowed = LOCAL_FILESYSTEMS | ({"tmpfs"} if root.startswith("/run/") else set())
        if filesystem not in allowed:
            raise Rejected("unsupported_install_storage")


def service_state(host, unit):
    status, output = host.command(
        (
            "/usr/bin/systemctl",
            "show",
            unit,
            "--no-pager",
            "--property=LoadState,ActiveState,UnitFileState",
        )
    )
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if status or values.get("LoadState") not in ("loaded", "not-found", "masked"):
        raise Rejected("service_state_unavailable")
    if values.get("ActiveState") not in ("active", "inactive", "failed"):
        raise Rejected("service_state_unavailable")
    return values


def service_check(host, seat):
    for unit in UNITS:
        if service_state(host, unit)["LoadState"] != "not-found":
            raise Rejected("existing_service_unrecognized")
    actions = []
    for unit, action in (
        ("getty@tty1.service", "reserve_tty1"),
        ("display-manager.service", "resolve_display_manager"),
    ):
        state = service_state(host, unit)
        if state["LoadState"] == "loaded":
            actions.append(action)
    if seat == "logind":
        state = service_state(host, "systemd-logind.service")
        if state["LoadState"] != "loaded" or state["ActiveState"] != "active":
            raise Rejected("logind_required")
        if not host.exists("/etc/pam.d/common-session"):
            raise Rejected("pam_required")
    else:
        actions.append("provision_seatd_socket_access")
    return tuple(actions)


def tool_check(host, package, executable):
    path = "/usr/bin/" + executable
    for directory in ("/usr/local/bin/", "/usr/local/sbin/"):
        if host.exists(directory + executable):
            raise Rejected("unsupported_tool_authority")
    installed = package_status(host, package)
    if not host.exists(path):
        if installed:
            raise Rejected("tool_unavailable")
        return Tool(package, path, "install_required")
    if not installed:
        raise Rejected("unsupported_tool_authority")
    owned(host, package, path)
    if executable == "chromium":
        # Browser wrappers can create per-user state even for version queries.
        status, output = host.command(
            (
                "/usr/bin/dpkg-query",
                "-W",
                "-f=${Version}",
                package,
            )
        )
    else:
        status, output = host.command((path, "--version"))
    match = re.search(r"\b(\d+)\.(\d+)(?:\.(\d+))?", output)
    if status or not match:
        raise Rejected("tool_version_invalid")
    if executable == "labwc" and tuple(int(n or 0) for n in match.groups()) < (0, 7, 1):
        raise Rejected("tool_version_unsupported")
    return Tool(package, path, "installed")


def chromium_snap(host):
    for path in (
        "/usr/local/bin/chromium",
        "/usr/local/bin/chromium-browser",
        "/usr/bin/chromium",
    ):
        if host.exists(path):
            raise Rejected("unsupported_tool_authority")
    if host.exists("/usr/bin/chromium-browser"):
        owned(host, "chromium-browser", "/usr/bin/chromium-browser")
    if not host.exists("/snap/bin/chromium"):
        return Tool("chromium", "/snap/bin/chromium", "install_required")
    owned(host, "snapd", "/usr/bin/snap")
    status, output = host.command(("/usr/bin/snap", "list", "chromium"))
    rows = output.splitlines()
    fields = rows[1].split() if len(rows) == 2 else []
    if (
        status
        or len(fields) != 6
        or fields[0] != "chromium"
        or not re.fullmatch(r"\d+(?:\.\d+)+", fields[1])
        or fields[3] != "latest/stable"
        or fields[4] not in ("canonical✓", "canonical**")
        or fields[5] != "-"
    ):
        raise Rejected("unsupported_tool_authority")
    if host.realpath("/snap/bin/chromium") != "/usr/bin/snap":
        raise Rejected("unsupported_tool_authority")
    return Tool("chromium", "/snap/bin/chromium", "installed")


def preflight(host=None, *, seat="logind"):
    host = host or Host()
    try:
        if seat not in ("logind", "seatd"):
            raise Rejected("unsupported_seat_authority")
        distro, release = identity(host)
        platform_check(host)
        python_version = python_check(host)
        storage_check(host)
        actions = service_check(host, seat)
        packages = BASE_PACKAGES + (("snapd",) if distro == "ubuntu" else ("chromium",))
        seat_packages = ("seatd",) if seat == "seatd" else ()
        for package in packages + seat_packages:
            package_available(host, package, distro)
        tools = tuple(tool_check(host, p, e) for p, e in TOOLS)
        browser = (
            chromium_snap(host)
            if distro == "ubuntu"
            else tool_check(host, "chromium", "chromium")
        )
        return Result(
            True,
            (),
            Plan(
                distro,
                release,
                "aarch64",
                True,
                "/usr/bin/python3",
                python_version,
                "ubuntu-archive-and-canonical-snap"
                if distro == "ubuntu"
                else "debian-rpi-archive",
                packages,
                seat_packages,
                seat,
                ("chromium", "latest/stable") if distro == "ubuntu" else (),
                tools + (browser,),
                actions,
            ),
        )
    except Rejected as exc:
        return Result(False, (str(exc),))
    except (OSError, ValueError, IndexError, KeyError, subprocess.SubprocessError):
        return Result(False, ("host_inspection_unavailable",))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--seat", choices=("logind", "seatd"), default="logind")
    args = parser.parse_args()
    result = preflight(seat=args.seat)
    if args.json:
        print(json.dumps(asdict(result), sort_keys=True))
    elif result.ok:
        print(
            "Preflight passed: clean ARM64 host; provisioning actions require installer handling."
        )
        print(
            "Apt packages: "
            + ", ".join(result.plan.apt_packages + result.plan.seat_packages)
        )
        print("Required actions: " + (", ".join(result.plan.actions) or "none"))
    else:
        print("Preflight failed: " + ", ".join(result.reasons))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
