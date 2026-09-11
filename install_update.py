"""Authenticated pre-mutation update inspection/staging; no public update lifecycle."""

import email.parser
import hashlib
import io
import os
import re
import stat
import zipfile
from dataclasses import dataclass

import postcardscene_install_host as host
import postcardscene_install_inputs as inputs
import postcardscene_install_update_state as state_files
from postcardscene_install_services import InstallError


@dataclass(frozen=True)
class Target:
    version: str
    wheel_name: str
    wheel: bytes
    requirements: bytes
    manifest_sha256: str
    identity: dict


def target_inputs(bundle, members):
    version, name, wheel, requirements = inputs.validate_inputs(
        bundle, members, host.ASSETS
    )
    manifest = inputs.read_input(bundle / inputs.MANIFEST_NAME, 64 * 1024)
    # Bind the manifest hash to precisely these authenticated members, including
    # helpers. Rereading must not silently substitute another candidate identity.
    if inputs.validate_manifest(bundle, members) != members:
        raise InstallError("update_target_changed")
    if inputs.read_input(bundle / inputs.MANIFEST_NAME, 64 * 1024) != manifest:
        raise InstallError("update_target_changed")
    return Target(
        version,
        name,
        wheel,
        requirements,
        hashlib.sha256(manifest).hexdigest(),
        inputs.inspect_wheel(wheel, name, host.ASSETS),
    )


def active_version():
    host.trusted_parent(host.ROOT)
    host.metadata(host.ROOT, mode=0o755, kind=stat.S_ISDIR)
    host.metadata(host.RELEASES, mode=0o755, kind=stat.S_ISDIR)
    active = host.ROOT / "venv"
    host.metadata(active, mode=0o777, kind=stat.S_ISLNK)
    target = active.readlink()
    version = target.parent.name
    if (
        not re.fullmatch(r"[0-9][a-z0-9.]{0,127}", version)
        or target != host.RELEASES / version / "venv"
    ):
        raise InstallError("managed_active_release_invalid")
    return version


def installed_distribution(release, version, wheel):
    """Inspect only root-controlled installed metadata; never run current code."""
    venv = release / "venv"
    sites = tuple((venv / "lib").glob("python3.*/site-packages"))
    if len(sites) != 1:
        raise InstallError("managed_distribution_invalid")
    site = sites[0]
    host.metadata(site, mode=0o755, kind=stat.S_ISDIR)
    package = site / "postcardscene"
    host.metadata(package, mode=0o755, kind=stat.S_ISDIR)
    distributions = []
    for path in site.iterdir():
        if re.match(r"postcardscene(?:-|[_.])", path.name, re.IGNORECASE):
            distributions.append(path)
    info = f"postcardscene-{version}.dist-info"
    if distributions != [site / info]:
        raise InstallError("managed_distribution_invalid")
    host.metadata(site / info, mode=0o755, kind=stat.S_ISDIR)
    with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
        for filename, fields in (
            ("METADATA", ("Name", "Version", "Requires-Python")),
            ("WHEEL", ("Wheel-Version", "Root-Is-Purelib", "Tag")),
        ):
            path = site / info / filename
            host.metadata(path)
            actual = email.parser.BytesParser().parsebytes(host.read_regular(path))
            expected = email.parser.BytesParser().parsebytes(
                archive.read(f"{info}/{filename}")
            )
            if any(actual.get_all(f) != expected.get_all(f) for f in fields):
                raise InstallError("managed_distribution_invalid")


def stored_release(version):
    release = host.RELEASES / version
    host.metadata(release, mode=0o755, kind=stat.S_ISDIR)
    name = f"postcardscene-{version}-py3-none-any.whl"
    if {p.name for p in release.iterdir()} != {
        name,
        "runtime-requirements.txt",
        "venv",
    }:
        raise InstallError("managed_release_invalid")
    host.metadata(release / name)
    wheel = host.read_regular(release / name, 32 * 1024 * 1024)
    identity = inputs.inspect_wheel(wheel, name, host.ASSETS)
    host.metadata(release / "runtime-requirements.txt")
    if not host.read_regular(release / "runtime-requirements.txt", 32 * 1024 * 1024):
        raise InstallError("managed_release_invalid")
    host.validate_tree(release, 0, 0, payload=True)
    installed_distribution(release, version, wheel)
    return identity, wheel


def validate_residue(target):
    release = host.RELEASES / target.version
    if active_version() == target.version:
        raise InstallError("active_release_is_not_staging")
    info = host.metadata(release, mode=0o755, kind=stat.S_ISDIR)
    if info.st_dev != host.RELEASES.lstat().st_dev:
        raise InstallError("unsafe_update_residue")
    names = {p.name for p in release.iterdir()}
    ordered = (target.wheel_name, "runtime-requirements.txt", "venv")
    if names not in [set(ordered[:n]) for n in range(4)]:
        raise InstallError("unsafe_update_residue")
    for name, content in (
        (target.wheel_name, target.wheel),
        ("runtime-requirements.txt", target.requirements),
    ):
        if name in names:
            host.metadata(release / name)
            if host.read_regular(release / name, 32 * 1024 * 1024) != content:
                raise InstallError("update_residue_identity_mismatch")
    if "venv" in names:
        host.metadata(release / "venv", mode=0o755, kind=stat.S_ISDIR)
    host.validate_tree(release, 0, 0, payload=True)
    return release


def require_state_target(state, current, target):
    if (state.from_version, state.from_wheel_sha256, state.from_schema) != (
        current["version"],
        current["wheel_sha256"],
        current["schema"],
    ) or (
        state.to_version,
        state.to_wheel_sha256,
        state.to_manifest_sha256,
        state.to_schema,
    ) != (
        target.version,
        target.identity["wheel_sha256"],
        target.manifest_sha256,
        target.identity["schema"],
    ):
        raise InstallError("update_state_identity_mismatch")


def recognize_current(target, preflight):
    """Target-specific inspection; ordinary install/remove classification is unchanged.

    Persisted phases here recognize intact old+target payloads and old assets only.
    Post-commit mixed activation/partial old cleanup remains #177's authority.
    """
    version = active_version()
    current, wheel = stored_release(version)
    if version == target.version:
        raise InstallError("update_requires_distinct_version")
    state = state_files.read()
    extra_roots = set()
    if state is not None:
        require_state_target(state, current, target)
        extra_roots.add(state_files.STATE)
    if os.path.lexists(state_files.SCRATCH):
        host.metadata(state_files.SCRATCH, mode=0o600)
        extra_roots.add(state_files.SCRATCH)
    release = host.RELEASES / target.version
    extra_releases = {release} if os.path.lexists(release) else set()
    host.installed_authority(
        version, wheel, extra_releases=extra_releases, extra_roots=extra_roots
    )
    host.conflict_record(preflight)
    host.service_authority(preflight)
    if state is None:
        host.require_auxiliary_installed(preflight)
    if extra_releases:
        validate_residue(target)
    if state is not None:
        staged, _ = stored_release(target.version)
        if staged != target.identity:
            raise InstallError("update_state_identity_mismatch")
    return current, state


def require_newer(python, current, target, run):
    # The host/bootstrap Python deliberately does not import packaging. Only the
    # authenticated, installed target environment supplies the PEP 440 parser.
    run(
        (
            python,
            "-I",
            "-B",
            "-c",
            "import sys; from pathlib import Path; import packaging.version as v; "
            "assert Path(v.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()); "
            "assert v.Version(sys.argv[2]) > v.Version(sys.argv[1])",
            current,
            target,
        )
    )


def discard_target(target, current_version, created=None):
    release = host.RELEASES / target.version
    if (
        active_version() != current_version
        or current_version == target.version
        or state_files.read() is not None
        or set(host.RELEASES.iterdir()) != {host.RELEASES / current_version, release}
    ):
        raise InstallError("update_cleanup_uncertain")
    if created is not None and not os.path.samestat(release.lstat(), created):
        raise InstallError("update_cleanup_uncertain")
    if created is None:
        validate_residue(target)
    host.discard_staged_payload(release)


def prepare_target(target, preflight, run=None):
    """Stage only; return identities to a later recovery gate, never write a phase."""
    if os.geteuid() != 0:
        raise InstallError("root_required")
    run = run or host.command
    state = state_files.read()
    if state is not None and state.phase == "committed":
        import postcardscene_install_update_finish as finish

        finish.recognize(target, preflight)
        current = dict(
            version=state.from_version,
            wheel_sha256=state.from_wheel_sha256,
            schema=state.from_schema,
        )
        return current, target, state
    current, state = recognize_current(target, preflight)
    result = preflight.preflight(installation="installed_managed")
    if not result.ok or any(t.state != "installed" for t in result.plan.tools):
        raise InstallError("update_preflight_rejected")
    inputs.require_forward_schema(current, target.identity)
    state_files.clean_scratch()
    release = host.RELEASES / target.version
    if state is not None:
        python = str(release / "venv/bin/python")
        require_newer(python, current["version"], target.version, run)
        return current, target, state
    if os.path.lexists(release):
        discard_target(target, current["version"])
    created = None

    def remember_created(info):
        nonlocal created
        created = info

    try:
        # stage_payload creates the directory first. Parent authority was proven
        # above; only root can change it. A failed mkdir never authorizes deletion.
        host.stage_payload(
            release,
            target.wheel_name,
            target.wheel,
            target.requirements,
            result.plan,
            run,
            on_created=remember_created,
        )
        stored, _ = stored_release(target.version)
        if stored != target.identity:
            raise InstallError("staged_target_invalid")
        require_newer(
            str(release / "venv/bin/python"), current["version"], target.version, run
        )
    except (Exception, KeyboardInterrupt) as error:
        if created is not None and os.path.lexists(release):
            try:
                discard_target(target, current["version"], created)
            except (OSError, InstallError) as cleanup:
                error.add_note(
                    "Staged target cleanup uncertain; preserve for inspection."
                )
                raise error from cleanup
        raise
    return current, target, None
