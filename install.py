"""Managed native install/remove; final published-bundle verification is #143."""

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
    "install_host.py": "51eef34dad46663b1a1ec3544b08bc5b678ed3537aa967aca2c982d39406a92c",
    "install_preflight.py": "b01b5e1f71325b44e1b8812da4d6132eb6ef70e64f14869f83de872036aeb107",
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


def bootstrap(python, run):
    # SQLite's initial 0644 creation mode cannot gain group write from umask.
    # Reserve only a new empty file as its runtime owner; Alembic owns all content.
    run(
        (
            python,
            "-I",
            "-c",
            "import os; fd=os.open('/var/lib/postcardscene/postcardscene.sqlite3', "
            "os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o660); os.close(fd)",
        ),
        user="postcardscene",
    )
    cli = (python, "-I", "-m", "flask", "--app", "postcardscene.web:create_app")
    run((*cli, "auth", "init-secret"), user="postcardscene-web")
    run((*cli, "db", "upgrade"), user="postcardscene-web")
    run((*cli, "db", "check"), user="postcardscene-web")
    run(
        (*cli, "auth", "create-admin"),
        user="postcardscene-web",
        interactive=True,
        timeout=900,
    )


def validate_preserved_application(python, run):
    # Only the private snapshot is opened by SQLite; no init, upgrade or admin CLI.
    run(
        (
            python,
            "-I",
            "-B",
            "-c",
            "import tempfile; from pathlib import Path; from sqlalchemy import select; "
            "from postcardscene.doctor.data import configuration, snapshot; "
            "from postcardscene.persistence import Database; "
            "from postcardscene.accounts import Administrator; "
            "configuration(); "
            "\nwith tempfile.TemporaryDirectory() as scratch:\n"
            " db=Database(snapshot(Path(scratch))); db.check()\n"
            " with db.transaction() as session:\n"
            "  assert session.scalar(select(Administrator.id).limit(1)) is not None\n"
            " db.engine.dispose()",
        ),
        timeout=60,
    )
    run(
        (
            python,
            "-I",
            "-B",
            "-c",
            "from postcardscene.session_secret import read_secret; "
            "read_secret('/var/lib/postcardscene-web/session.key')",
        ),
        user="postcardscene-web",
    )


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
        state = host.classify(version, wheel, preflight)
        if state not in ("clean", "removed_preserved"):
            raise InstallError("partial_or_unknown_manual_reconciliation_required")
        if state == "removed_preserved":
            return self._reinstall(
                version, wheel_name, wheel, requirements, preflight, host
            )
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
        bootstrap(python, self.run)
        self.phase = "activation"
        host.activate_payload(self.release)
        self.activation = True
        self.run(("/usr/bin/systemctl", "daemon-reload"))
        self.run(("/usr/bin/systemctl", "enable", *self.host.SERVICES))
        for unit in host.SERVICES:
            self.run(("/usr/bin/systemctl", "start", unit))
        self.run(("/usr/bin/systemctl", "is-active", "--quiet", *self.host.SERVICES))
        self.phase = "complete"

    def _reinstall(self, version, wheel_name, wheel, requirements, preflight, host):
        if os.geteuid() != 0:
            raise InstallError("root_required")
        result = preflight.preflight(installation="preserved")
        if not result.ok:
            raise InstallError("preflight_rejected")
        self.phase = "packages"
        host.install_packages(result.plan, self.run)
        if host.classify(version, wheel, preflight) != "removed_preserved":
            raise InstallError("partial_or_unknown_manual_reconciliation_required")
        result = preflight.preflight(installation="preserved")
        if not result.ok or any(t.state != "installed" for t in result.plan.tools):
            raise InstallError("post_package_preflight_rejected")
        self.phase = "payload"
        host.directory(Path("/opt/postcardscene/releases"))
        self.release = Path("/opt/postcardscene/releases") / version
        python = host.stage_payload(
            self.release, wheel_name, wheel, requirements, result.plan, self.run
        )
        self.phase = "preserved_compatibility"
        validate_preserved_application(python, self.run)
        runtime, _, shared, _ = host.preserved_authority()
        host.require_no_processes(host.preserved_identities())
        self.phase = "reprovisioning"
        # From here installed assets may refer to the staged payload. Preserve it
        # on failure and stop services; durable data is never bootstrapped.
        self.durable = True
        host.directory(Path("/var/cache/postcardscene"), runtime, shared, 0o700)
        host.reserve_graphics(preflight, self.run, preserved=True)
        host.install_assets(wheel, self.run)
        host.require_stopped(preflight)
        self.phase = "activation"
        host.activate_payload(self.release)
        self.activation = True
        self.run(("/usr/bin/systemctl", "daemon-reload"))
        self.run(("/usr/bin/systemctl", "enable", *host.SERVICES))
        for unit in host.SERVICES:
            self.run(("/usr/bin/systemctl", "start", unit))
        self.run(("/usr/bin/systemctl", "is-active", "--quiet", *host.SERVICES))
        if host.classify(version, wheel, preflight) != "installed_managed":
            raise InstallError("reinstalled_authority_incoherent")
        self.phase = "complete"

    def remove(self):
        version, _, wheel, _, preflight, self.host = validate_inputs(self.bundle)
        if self.run is None:
            self.run = self.host.command
        if os.geteuid() != 0:
            raise InstallError("root_required")
        try:
            state = self.host.classify(version, wheel, preflight)
            if state == "removed_preserved":
                self.phase = "already_removed_preserved"
                return
            if state != "installed_managed":
                raise InstallError("partial_or_unknown_manual_reconciliation_required")
            validate_preserved_application(
                str(Path("/opt/postcardscene/venv/bin/python")), self.run
            )
            self.phase = "removal"
            self.host.remove_managed(version, wheel, preflight, self.run)
            if self.host.classify(version, wheel, preflight) != "removed_preserved":
                raise InstallError("removal_incomplete_manual_reconciliation_required")
            self.phase = "removed_preserved"
        except (self.host.InstallError, preflight.Rejected) as error:
            raise InstallError(str(error)) from None

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
            except (OSError, self.host.InstallError):
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
    parser.add_argument("action", choices=("install", "remove"))
    args = parser.parse_args()
    operation = Installation(Path(__file__).resolve().parent)
    try:
        getattr(operation, args.action)()
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
        if args.action == "install":
            operation.recover()
        else:
            print(
                "Removal incomplete; preserve remaining targets and reconcile manually.",
                file=sys.stderr,
            )
        return 1
    if args.action == "remove":
        print(
            f"{operation.phase}: preserved config, durable database/admin, private signing key, backups, service identities and /opt/postcardscene/service-conflicts.json. Host packages retained."
        )
    else:
        print(
            "Installation complete; services active. Physical display readiness is unverified."
        )
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
