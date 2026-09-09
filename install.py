"""Deliberate clean native installation; final published-bundle verification is #143."""

import argparse
import configparser
import csv
import hashlib
import importlib.util
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

# Reviewed #138/#139 input pins, checked against source by release-input validation.
# These are installer inputs, not a release manifest or published-bundle schema.
INPUT_HASHES = {
    "install_inputs.py": "e30622cb3258479669f0a32ab06924b1b37dfa7151cb293c749859f675711218",
    "install_host.py": "f61201bf57bd4597b7ac198b2090276023560a714821022e0a12293efd35eb37",
    "install_preflight.py": "2dfd1d5df53ec933ed4fb38a1edbe4309b5dd4cff901a9a5de81a5c6ff050c57",
    "runtime-requirements.txt": "ca8eb8d430bd3d883523e592c99bec74c65c7537a765c52998001f0ae4c76d3a",
}


class InstallError(Exception):
    """Only fixed safe reasons cross the installer output boundary."""


def read_input(path, limit=32 * 1024 * 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise InstallError("invalid_bundle_input")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise InstallError("invalid_bundle_input")
        return data


def load_support(bundle):
    # This minimal bootstrap read cannot depend on code it has yet to authenticate.
    # Authenticate the whole fixed input set before executing any support bytes.
    inputs = {name: read_input(bundle / name) for name in INPUT_HASHES}
    for name, digest in INPUT_HASHES.items():
        if hashlib.sha256(inputs[name]).hexdigest() != digest:
            raise InstallError("install_support_mismatch")
    modules = []
    for name in ("install_inputs.py", "install_host.py", "install_preflight.py"):
        spec = importlib.util.spec_from_loader(
            "postcardscene_" + name[:-3], loader=None
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        exec(compile(inputs[name], name, "exec"), module.__dict__)
        modules.append(module)
    return *modules, inputs["runtime-requirements.txt"]


def validate_inputs(bundle):
    inputs, host, preflight, requirements = load_support(bundle)
    try:
        validated = inputs.validate_inputs(bundle, requirements, host.ASSETS)
    except inputs.InstallError as error:
        raise InstallError(str(error)) from None
    return *validated, preflight, host


class Installation:
    def __init__(self, bundle, run=None):
        self.bundle = bundle
        self.run = run
        self.host = None
        self.phase = "validation"
        self.release = None
        self.durable = False
        self.activation = False

    def install(self):
        validated = validate_inputs(self.bundle)
        self.host = validated[-1]
        if self.run is None:
            self.run = self.host.command
        try:
            self._install(*validated)
        except self.host.InstallError as error:
            raise InstallError(str(error)) from None

    def _install(self, version, wheel_name, wheel, requirements, preflight, host):
        result = preflight.preflight()
        if not result.ok:
            raise InstallError("preflight_rejected")
        if os.geteuid() != 0 or not sys.stdin.isatty():
            raise InstallError("root_interactive_terminal_required")
        self.phase = "packages"
        host.install_packages(result.plan, self.run)
        # Repeat the read-only clean-host gate after package changes, before state.
        result = preflight.preflight()
        if not result.ok or any(t.state != "installed" for t in result.plan.tools):
            raise InstallError("post_package_preflight_rejected")
        self.phase = "payload"
        host.directory(Path("/opt/postcardscene"))
        host.directory(Path("/opt/postcardscene/releases"))
        self.release = Path("/opt/postcardscene/releases") / version
        python = host.stage_payload(
            self.release, wheel_name, wheel, requirements, result.plan, self.run
        )
        self.phase = "identities_and_roots"
        runtime, web, shared, private = host.provision_identities(self.run)
        host.directory(Path("/etc/postcardscene"), gid=shared, mode=0o750)
        host.write_new(Path("/etc/postcardscene/config.py"), host.CONFIG, 0o640, shared)
        host.directory(Path("/var/lib/postcardscene"), runtime, shared, 0o2770)
        host.directory(Path("/var/lib/postcardscene-web"), web, private, 0o700)
        host.directory(Path("/var/cache/postcardscene"), runtime, shared, 0o700)
        self.phase = "assets"
        host.install_assets(wheel, self.run)
        host.reserve_graphics(preflight, self.run)
        # Fresh services have never been enabled/started. Recheck their state
        # without an early daemon-reload before durable mutation.
        host.require_stopped(preflight)
        self.phase = "bootstrap"
        self.durable = True
        host.bootstrap(python, self.run)
        self.phase = "activation"
        host.activate_payload(self.release)
        self.activation = True
        self.run(("/usr/bin/systemctl", "daemon-reload"))
        self.run(("/usr/bin/systemctl", "enable", *self.host.SERVICES))
        for unit in host.SERVICES:
            self.run(("/usr/bin/systemctl", "start", unit))
        self.run(("/usr/bin/systemctl", "is-active", "--quiet", *self.host.SERVICES))
        self.phase = "complete"

    def recover(self):
        if self.durable:
            if self.activation:
                try:
                    self.run(("/usr/bin/systemctl", "disable", *self.host.SERVICES))
                except (
                    OSError,
                    InstallError,
                    self.host.InstallError,
                    subprocess.SubprocessError,
                ):
                    print(
                        "Disable failed; prevent automatic service startup before recovery.",
                        file=sys.stderr,
                    )
            try:
                self.run(("/usr/bin/systemctl", "stop", *self.host.SERVICES))
            except (
                OSError,
                InstallError,
                self.host.InstallError,
                subprocess.SubprocessError,
            ):
                print(
                    "Service stop failed; inspect and stop installed units before recovery.",
                    file=sys.stderr,
                )
        elif self.release is not None:
            # Exact fresh root-controlled payload only; never durable/config authority.
            try:
                self.host.discard_staged_payload(self.release)
            except OSError:
                print(
                    "Staged payload cleanup failed; preserve it for inspection.",
                    file=sys.stderr,
                )
        print(
            f"Install failed during {self.phase}. Preserve config, state and signing key. "
            "Inspect host package/service status and the managed-install recovery instructions; "
            "do not delete state or rerun as an update.",
            file=sys.stderr,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install",))
    parser.parse_args()
    operation = Installation(Path(__file__).resolve().parent)
    try:
        operation.install()
    except (
        InstallError,
        OSError,
        ValueError,
        KeyError,
        configparser.Error,
        csv.Error,
        zipfile.BadZipFile,
        subprocess.SubprocessError,
        KeyboardInterrupt,
    ) as error:
        if isinstance(error, InstallError):
            print("Install reason: " + str(error), file=sys.stderr)
        operation.recover()
        return 1
    print(
        "Installation complete; services active. Physical display readiness is unverified."
    )
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
