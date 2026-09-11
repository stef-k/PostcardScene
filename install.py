"""Managed native install/remove from an externally checksum-verified release."""

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
    "install_inputs.py": "99adeb5e24ab1311ef4538e0b331b8bc0f3a70548f2738c27bc9bd67961efe9a",
    "install_services.py": "0974c2f102098dfcb5a6ab1a71d18930bd1b28bb02f4eab8e62e7065a2245ee8",
    "install_host.py": "6b6fbc920304059a68ae4026f1697b49b191d2f1d6fcb0861e6fc8c20ef6d0d6",
    "install_preflight.py": "634d686f7267e56a297b129a92acbcc7a28ca4b6ebf6532ef85de5334aa63b2f",
    "install_update_state.py": "5979eea7b598c27b157e5f8f91230453d8d2430c1e2c613ea2f8ef3b53ece5d9",
    "install_update.py": "8565bcb3d9ed68b416e3b23c82c1a0dc7fb89a31299e7030b0e4e623305c7e58",
    "runtime-requirements.txt": "ad6fd58dc71d7fe2b6da10432cf8bd4ed3f485231f1ed72392aad9624de78deb",
    "install_update_recovery.py": "9f73ea1c7af10b1c9c7c87fe0c46fb0b0bcdc23ccc11c6f6ada20e7d26577743",
    "install_update_database.py": "6a165ead4c9911578be1d297d29aae8cfc7139287659197d1abfc4410cd9bc3a",
    "install_update_transaction.py": "0424baf5491f6f76147de1b0c40d8460f545fac579f1d3abaf37cb4dbba9cf2e",
    "install_update_assets.py": "073f31cd9c404f5f9749eeb4583cc76425685df949b1625a109e690e80646a6c",
    "install_update_finish.py": "36657eb38e8141f7c0dcf7210c3ded1f7fdf17629840a08bd4afe7a52c042a3c",
    "install_command.py": "dbacdbca6c89f46ea4d4cfd52c72cc3a74e4111cbaaf0de51dd74479d770a653",
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
    members = None
    for name in (
        "install_inputs.py",
        "install_services.py",
        "install_command.py",
        "install_host.py",
        "install_preflight.py",
        "install_update_state.py",
        "install_update.py",
        "install_update_recovery.py",
        "install_update_database.py",
        "install_update_transaction.py",
        "install_update_assets.py",
        "install_update_finish.py",
    ):
        spec = importlib.util.spec_from_loader(
            "postcardscene_" + name[:-3], loader=None
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        exec(compile(inputs[name], name, "exec"), module.__dict__)
        if name in ("install_inputs.py", "install_host.py", "install_preflight.py"):
            modules.append(module)
        if name == "install_inputs.py":
            try:
                members = module.validate_manifest(bundle, inputs)
            except module.InstallError as error:
                raise InstallError(str(error)) from None
    return *modules, members


def validate_inputs(bundle):
    inputs, host, preflight, members = load_support(bundle)
    try:
        validated = inputs.validate_inputs(bundle, members, host.ASSETS)
    except inputs.InstallError as error:
        raise InstallError(str(error)) from None
    return *validated, preflight, host


def prepare_update(bundle, run=None):
    """Internal #175 staging seam; no CLI action or recovery authorization."""
    _, _, preflight, members = load_support(bundle)
    update = sys.modules["postcardscene_install_update"]
    return update.prepare_target(update.target_inputs(bundle, members), preflight, run)


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
        self.reinstall_parent = None
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
        host.bootstrap(python, self.run)
        self.phase = "activation"
        host.activate_payload(self.release)
        self.activation = True
        self.run(("/usr/bin/systemctl", "daemon-reload"))
        self.run(("/usr/bin/systemctl", "enable", *self.host.SERVICES))
        for unit in host.SERVICES:
            self.run(("/usr/bin/systemctl", "start", unit))
        timer = host.AUXILIARY_UNITS[1]
        self.run(("/usr/bin/systemctl", "enable", timer))
        self.run(("/usr/bin/systemctl", "start", timer))
        self.run(("/usr/bin/systemctl", "is-active", "--quiet", timer))
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
        self.reinstall_parent = host.directory(host.RELEASES)
        self.release = host.RELEASES / version
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
        timer = host.AUXILIARY_UNITS[1]
        self.run(("/usr/bin/systemctl", "enable", timer))
        self.run(("/usr/bin/systemctl", "start", timer))
        self.run(("/usr/bin/systemctl", "is-active", "--quiet", timer))
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

    def update(self, destination=None, archive=None):
        _, host, preflight, members = load_support(self.bundle)
        self.host = host
        self.phase = "update"
        update = sys.modules["postcardscene_install_update"]
        finish = sys.modules["postcardscene_install_update_finish"]
        try:
            target = update.target_inputs(self.bundle, members)
            finish.execute(
                target,
                preflight,
                destination=destination,
                archive=archive,
                run=self.run,
            )
        except (
            host.InstallError,
            update.inputs.InstallError,
            preflight.Rejected,
        ) as error:
            raise InstallError(str(error)) from None
        self.phase = "complete"

    def recover(self):
        if self.durable:
            if self.activation:
                try:
                    self.host.stop_auxiliary(self.run)
                except (
                    OSError,
                    InstallError,
                    self.host.InstallError,
                    subprocess.SubprocessError,
                ):
                    print(
                        "Auxiliary stop/disable failed; prevent scheduled execution before recovery.",
                        file=sys.stderr,
                    )
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
                if self.reinstall_parent is not None:
                    self.host.discard_reinstall_staging(
                        self.release, self.reinstall_parent
                    )
                else:
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
    parser.add_argument("action", choices=("install", "remove", "update"))
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument("--backup-destination")
    recovery.add_argument("--recovery-archive")
    args = parser.parse_args()
    if args.action != "update" and (args.backup_destination or args.recovery_archive):
        parser.error("Recovery options require update.")
    operation = Installation(Path(__file__).resolve().parent)
    try:
        if args.action == "update":
            operation.update(args.backup_destination, args.recovery_archive)
        else:
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
        elif args.action == "update":
            print(
                "Update incomplete; preserve state and rerun this exact target bundle. "
                "Committed updates finish forward only.",
                file=sys.stderr,
            )
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
