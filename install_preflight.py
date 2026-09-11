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
    "postcardscene-backup.service",
    "postcardscene-backup.timer",
)
UNIT_FILE_STATES = {
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
        try:
            os.lstat(path)
        except FileNotFoundError:
            return False
        return True

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
                    # Observe exit without reaping, retaining process-group authority.
                    while time.monotonic() < deadline:
                        result = os.waitid(
                            os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG
                        )
                        if result is not None:
                            status = (
                                result.si_status
                                if result.si_code == os.CLD_EXITED
                                else -result.si_status
                            )
                            return status, data.decode("utf-8", "strict")
                        time.sleep(0.01)
                    break
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


def package_available(host, package, distro, release):
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
    # Candidate and installed versions must both have an approved cached origin.
    candidate = re.search(r"Candidate: (\S+)", output).group(1)
    installed = re.search(r"Installed: (\S+)", output)
    versions = {candidate}
    if installed and installed.group(1) != "(none)":
        versions.add(installed.group(1))
    domains = (
        r"(?:ports|archive|security)\.ubuntu\.com"
        if distro == "ubuntu"
        else r"(?:deb|security)\.debian\.org|archive\.raspberrypi\.com"
    )
    for version in versions:
        section = re.search(
            r"^ +(?:\*\*\* )?" + re.escape(version) + r" \d+\n((?: {8,}.*\n?)*)",
            output,
            re.MULTILINE,
        )
        if not section:
            raise Rejected("unsupported_package_authority")
        sources = [
            line.split()
            for line in section.group(1).splitlines()
            if "/var/lib/dpkg/status" not in line
        ]
        codename = {"24.04": "noble", "26.04": "resolute", "13": "trixie"}[release]
        for source in sources:
            if (
                len(source) != 5
                or not re.fullmatch(r"https?://(?:" + domains + r")/\S*", source[1])
                or not re.fullmatch(
                    codename + r"(?:-updates|-security|-backports)?/\S+", source[2]
                )
                or source[3:] != ["arm64", "Packages"]
            ):
                raise Rejected("unsupported_package_authority")
        if not sources:
            raise Rejected("unsupported_package_authority")
        if package == "labwc":
            number = re.match(r"(?:\d+:)?(\d+)\.(\d+)\.(\d+)", version)
            if not number or tuple(map(int, number.groups())) < (0, 7, 1):
                raise Rejected("tool_version_unsupported")


def python_check(host):
    if not package_status(host, "python3") or not package_status(host, "python3-venv"):
        raise Rejected("python_bootstrap_required")
    owned(host, "python3-minimal", "/usr/bin/python3")
    resolved = host.realpath("/usr/bin/python3")
    if not re.fullmatch(r"/usr/bin/python3\.\d+", resolved):
        raise Rejected("python_bootstrap_required")
    owned(host, Path(resolved).name + "-minimal", resolved)
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
    except (ValueError, TypeError, AttributeError):
        valid = False
    if status or not valid:
        raise Rejected("python_bootstrap_required")
    return ".".join(map(str, version))


def storage_check(host, installation="clean"):
    mounts = []
    for line in host.read("/proc/self/mountinfo").splitlines():
        fields = line.split()
        separator = fields.index("-")
        path = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])
        mounts.append((path, fields[separator + 1], "rw" in fields[5].split(",")))
    for root in ROOTS:
        filesystem = max(
            (
                (p, fs, writable)
                for p, fs, writable in mounts
                if root == p or root.startswith(p.rstrip("/") + "/")
            ),
            key=lambda item: len(item[0]),
            default=("", "unknown", False),
        )
        if not filesystem[2]:
            raise Rejected("unsupported_install_storage")
        filesystem = filesystem[1]
        allowed = LOCAL_FILESYSTEMS | ({"tmpfs"} if root.startswith("/run/") else set())
        if filesystem not in allowed:
            raise Rejected("unsupported_install_storage")

        path = Path(root)
        for ancestor in reversed(
            path.parents if installation != "clean" else (path, *path.parents)
        ):
            if not host.exists(str(ancestor)):
                continue
            info = host.lstat(str(ancestor))
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0
                or info.st_mode & 0o022
            ):
                raise Rejected("unsafe_install_root")
        if installation == "clean" and host.exists(root):
            # Standalone inspection never adopts even empty roots.
            raise Rejected("existing_installation_unrecognized")


def service_state(host, unit):
    status, output = host.command(
        (
            "/usr/bin/systemctl",
            "show",
            unit,
            "--no-pager",
            "--all",
            "--property=LoadState,ActiveState,UnitFileState",
        )
    )
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if status or values.get("LoadState") not in ("loaded", "not-found", "masked"):
        raise Rejected("service_state_unavailable")
    active_states = {"active", "inactive", "failed"}
    if unit == "postcardscene-backup.service":
        active_states.add("activating")  # A running Type=oneshot remains activating.
    if values.get("ActiveState") not in active_states:
        raise Rejected("service_state_unavailable")
    if values.get("UnitFileState") not in UNIT_FILE_STATES:
        raise Rejected("service_state_unavailable")
    return values


def unit_names(host):
    names = set()
    for operation in ("list-unit-files", "list-units"):
        status, output = host.command(
            (
                "/usr/bin/systemctl",
                operation,
                "--all",
                "--no-legend",
                "--plain",
                "--no-pager",
                "postcardscene*",
            )
        )
        # list-unit-files returns 1 for an empty pattern match.
        empty = operation == "list-unit-files" and status == 1 and not output.strip()
        if status and not empty:
            raise Rejected("service_state_unavailable")
        names.update(line.split()[0] for line in output.splitlines())
    return names


def service_check(host, seat, installation="clean"):
    for path in ("/etc/passwd", "/etc/group"):
        if installation == "clean" and any(
            line.split(":", 1)[0] in ("postcardscene", "postcardscene-web")
            for line in host.read(path).splitlines()
        ):
            raise Rejected("existing_installation_unrecognized")
    # systemd can retain inactive not-found entries after daemon-reload.
    allowed = set(UNITS) if installation != "clean" else set()
    if unit_names(host) - allowed:
        raise Rejected("existing_service_unrecognized")
    for unit in UNITS:
        state = service_state(host, unit)
        if installation == "installed_managed":
            if state["LoadState"] != "loaded":
                raise Rejected("existing_service_unrecognized")
            continue
        if (
            state["LoadState"] != "not-found"
            or state["ActiveState"] != "inactive"
            or state["UnitFileState"] != ""
        ):
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
    if not host.lstat(host.realpath(path)).st_mode & 0o111:
        raise Rejected("tool_unavailable")
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
        options = ("--no-config", "--load-scripts=no") if executable == "mpv" else ()
        status, output = host.command((path, *options, "--version"))
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
        if host.exists("/snap/chromium/current"):
            raise Rejected("tool_unavailable")
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
    if host.lstat("/snap/bin/chromium").st_uid != 0:
        raise Rejected("unsupported_tool_authority")
    if host.realpath("/snap/bin/chromium") != "/usr/bin/snap":
        raise Rejected("unsupported_tool_authority")
    return Tool("chromium", "/snap/bin/chromium", "installed")


def preflight(host=None, *, seat="logind", installation="clean"):
    # Internal prerequisite reuse only. The installer must separately recognize
    # removed_preserved/installed_managed before these modes; no CLI switch.
    host = host or Host()
    try:
        if installation not in ("clean", "preserved", "installed_managed"):
            raise Rejected("unsupported_installation_mode")
        if seat not in ("logind", "seatd"):
            raise Rejected("unsupported_seat_authority")
        distro, release = identity(host)
        platform_check(host)
        python_version = python_check(host)
        storage_check(host, installation)
        actions = service_check(host, seat, installation)
        packages = BASE_PACKAGES + (("snapd",) if distro == "ubuntu" else ("chromium",))
        seat_packages = ("seatd",) if seat == "seatd" else ()
        for package in packages + seat_packages:
            package_available(host, package, distro, release)
        if installation == "installed_managed" and any(
            not package_status(host, package) for package in packages + seat_packages
        ):
            raise Rejected("update_prerequisites_missing")
        tools = tuple(tool_check(host, p, e) for p, e in TOOLS)
        browser = (
            chromium_snap(host)
            if distro == "ubuntu"
            else tool_check(host, "chromium", "chromium")
        )
        if installation == "installed_managed" and any(
            tool.state != "installed" for tool in (*tools, browser)
        ):
            raise Rejected("update_prerequisites_missing")
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
                installation,
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
        if result.plan.snap:
            print("Snap: " + " ".join(result.plan.snap))
        print(
            "Missing tools: "
            + (
                ", ".join(
                    tool.executable
                    for tool in result.plan.tools
                    if tool.state == "install_required"
                )
                or "none"
            )
        )
        print("Required actions: " + (", ".join(result.plan.actions) or "none"))
    else:
        print("Preflight failed: " + ", ".join(result.reasons))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
