"""Root file authority and interruption contracts; no live services or database I/O."""

import io
import os
import stat
import sys
import zipfile
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_native_install import bundle as bundle
from test_native_install import installer

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0, reason="requires root file ownership"
)


@pytest.fixture
def managed(bundle, tmp_path, monkeypatch):
    installer.load_support(bundle)
    update = sys.modules["postcardscene_install_update"]
    host = update.host
    root = tmp_path / "managed"
    root.mkdir(mode=0o755)
    releases = root / "releases"
    releases.mkdir()
    monkeypatch.setattr(host, "ROOT", root)
    monkeypatch.setattr(host, "RELEASES", releases)
    monkeypatch.setattr(host, "MARKER", root / "service-conflicts.json")
    for name, path in (
        ("ROOT", root),
        ("STATE", root / "update-state.json"),
        ("SCRATCH", root / "update-state.json.postcardscene-update.new"),
    ):
        monkeypatch.setattr(update.state_files, name, path)
    # Only the disposable pytest ancestor is exempt from production /opt parent
    # checks. All managed-tree ownership/modes/link/tree checks remain real.
    original_parent = host.trusted_parent

    def trusted_parent(path):
        if path.is_relative_to(tmp_path):
            for parent in path.parents:
                if parent == tmp_path:
                    break
                host.metadata(parent, mode=0o755, kind=stat.S_ISDIR)
        else:
            original_parent(path)

    monkeypatch.setattr(host, "trusted_parent", trusted_parent)
    monkeypatch.setattr(update.state_files, "trusted_parent", trusted_parent)
    _, _, _, members = installer.load_support(bundle)
    target = update.target_inputs(bundle, members)
    current_version = target.version
    # A second release identity uses the same authentic source fixture wheel
    # with a different wheel name and metadata, with RECORD rebuilt below.
    target = renamed_target(update, target, "0.2.0")
    current_wheel = members[next(n for n in members if n.endswith(".whl"))]
    current = releases / current_version
    current.mkdir()
    populate_release(
        current, current_version, current_wheel, members["runtime-requirements.txt"]
    )
    (root / "venv").symlink_to(current / "venv")
    host.MARKER.write_text("{}")
    assets_path = tmp_path / "assets"
    assets_path.mkdir()
    assets = {assets_path / "unit.service": b"old-unit"}
    for path, content in assets.items():
        path.write_bytes(content)
    monkeypatch.setattr(host, "asset_bytes", lambda wheel: assets)
    monkeypatch.setattr(host, "DROPINS", ())
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    monkeypatch.setattr(host, "CACHE", cache)
    monkeypatch.setattr(host, "TRANSIENTS", (tmp_path / "run", tmp_path / "wayland"))
    # Existing durable and systemd seams have their own tests; forbid any live
    # database or service invocation in this payload/state authority fixture.
    monkeypatch.setattr(host, "preserved_authority", lambda: (0, 0, 0, 0))
    monkeypatch.setattr(host, "conflict_record", lambda pf: {})
    monkeypatch.setattr(host, "service_authority", lambda pf: None)
    monkeypatch.setattr(host, "require_auxiliary_installed", lambda pf: None)
    plan = SimpleNamespace(python="/usr/bin/python3", tools=())
    preflight = SimpleNamespace(
        preflight=lambda **kw: SimpleNamespace(ok=True, plan=plan)
    )
    return update, target, current, preflight


def renamed_target(update, target, version):
    import base64
    import csv
    import hashlib

    with zipfile.ZipFile(io.BytesIO(target.wheel)) as archive:
        payload = {
            n.replace(
                target.version + ".dist-info", version + ".dist-info"
            ): archive.read(n)
            for n in archive.namelist()
            if not n.endswith("/RECORD")
        }
    info = f"postcardscene-{version}.dist-info"
    payload[f"{info}/METADATA"] = payload[f"{info}/METADATA"].replace(
        target.version.encode(), version.encode()
    )
    record = io.StringIO()
    writer = csv.writer(record)
    for name, content in payload.items():
        if not name.endswith("/"):
            writer.writerow(
                (
                    name,
                    "sha256="
                    + base64.urlsafe_b64encode(hashlib.sha256(content).digest())
                    .decode()
                    .rstrip("="),
                    len(content),
                )
            )
    writer.writerow((f"{info}/RECORD", "", ""))
    payload[f"{info}/RECORD"] = record.getvalue().encode()
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name, content in payload.items():
            archive.writestr(name, content)
    wheel = data.getvalue()
    name = f"postcardscene-{version}-py3-none-any.whl"
    return replace(
        target,
        version=version,
        wheel_name=name,
        wheel=wheel,
        identity=update.inputs.inspect_wheel(wheel, name, update.host.ASSETS),
    )


def populate_release(release, version, wheel, requirements):
    (release / f"postcardscene-{version}-py3-none-any.whl").write_bytes(wheel)
    (release / "runtime-requirements.txt").write_bytes(requirements)
    site = release / "venv/lib/python3.12/site-packages"
    site.mkdir(parents=True)
    (site / "postcardscene").mkdir()
    with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
        info = f"postcardscene-{version}.dist-info"
        (site / info).mkdir()
        for name in ("METADATA", "WHEEL"):
            (site / info / name).write_bytes(archive.read(f"{info}/{name}"))


def test_current_authority_and_ordinary_layout_remain_exact(managed):
    update, target, current, preflight = managed
    identity, state = update.recognize_current(target, preflight)
    assert identity["version"] == current.name
    assert state is None
    wheel = next(current.glob("*.whl")).read_bytes()
    update.host.installed_authority(current.name, wheel)
    (update.host.RELEASES / target.version).mkdir()
    update.recognize_current(target, preflight)
    with pytest.raises(update.InstallError):
        update.host.installed_authority(current.name, wheel)


@pytest.mark.parametrize(
    "damage",
    [
        "metadata",
        "tag",
        "extra_dist",
        "extra_wheel",
        "extra_release",
        "extra_root",
        "active",
        "asset",
        "requirements",
    ],
)
def test_foreign_or_mixed_authority_refused(managed, damage):
    update, target, current, preflight = managed
    site = current / "venv/lib/python3.12/site-packages"
    if damage == "metadata":
        (site / f"postcardscene-{current.name}.dist-info/METADATA").write_text(
            "Name: postcardscene\nVersion: 99\n"
        )
    elif damage == "tag":
        (site / f"postcardscene-{current.name}.dist-info/WHEEL").write_text(
            "Tag: foreign\n"
        )
    elif damage == "extra_dist":
        (site / "postcardscene-99.dist-info").mkdir()
    elif damage == "extra_wheel":
        (current / "foreign.whl").write_bytes(b"foreign")
    elif damage == "extra_release":
        (update.host.RELEASES / "99").mkdir()
    elif damage == "extra_root":
        (update.host.ROOT / "foreign").touch()
    elif damage == "active":
        (update.host.ROOT / "venv").unlink()
        (update.host.ROOT / "venv").symlink_to(current / "../" / current.name / "venv")
    elif damage == "asset":
        next(iter(update.host.asset_bytes(b""))).write_bytes(b"foreign")
    else:
        (current / "runtime-requirements.txt").unlink()
    with pytest.raises((update.InstallError, OSError)):
        update.recognize_current(target, preflight)
    assert current.exists()


@pytest.mark.parametrize(
    "boundary", ["empty", "wheel", "requirements", "partial_venv", "full"]
)
def test_hard_staging_interruption_is_same_target_only(managed, monkeypatch, boundary):
    update, target, current, preflight = managed
    host = update.host
    release = host.RELEASES / target.version
    original_write = host.write_new

    class Killed(BaseException):
        pass

    def write(path, content, *args):
        if boundary == "empty":
            raise Killed
        original_write(path, content, *args)
        if boundary == "wheel" and path.name == target.wheel_name:
            raise Killed
        if boundary == "requirements" and path.name == "runtime-requirements.txt":
            raise Killed

    def run(args, **kwargs):
        if args[1:4] == ("-I", "-m", "venv"):
            if boundary == "partial_venv":
                (release / "venv").mkdir()
                (release / "venv/partial").touch()
            else:
                populate_release(
                    release, target.version, target.wheel, target.requirements
                )
            raise Killed

    monkeypatch.setattr(host, "write_new", write)
    with pytest.raises(Killed):
        update.prepare_target(target, preflight, run)
    identity, state = update.recognize_current(target, preflight)
    assert state is None and identity["version"] == current.name
    assert update.active_version() == current.name
    update.discard_target(target, current.name)
    assert not release.exists() and current.exists()
    assert update.state_files.read() is None


@pytest.mark.parametrize(
    "damage",
    [
        "foreign_wheel",
        "wrong_requirements",
        "unexpected",
        "symlink",
        "hardlink",
        "mode",
        "foreign_owner",
        "prefix",
    ],
)
def test_uncertain_residue_is_never_deleted(managed, damage):
    update, target, current, preflight = managed
    release = update.host.RELEASES / target.version
    release.mkdir()
    (release / target.wheel_name).write_bytes(target.wheel)
    if damage == "foreign_wheel":
        (release / target.wheel_name).write_bytes(b"other")
    elif damage == "wrong_requirements":
        (release / "runtime-requirements.txt").write_bytes(b"other")
    elif damage == "unexpected":
        (release / "other").touch()
    elif damage == "symlink":
        (release / target.wheel_name).unlink()
        (release / target.wheel_name).symlink_to(next(current.glob("*.whl")))
    elif damage == "hardlink":
        (release / target.wheel_name).unlink()
        os.link(next(current.glob("*.whl")), release / target.wheel_name)
    elif damage == "mode":
        release.chmod(0o777)
    elif damage == "foreign_owner":
        # A foreign uid is unmapped in a one-uid local user namespace.
        try:
            os.chown(release, 1, 1)
        except OSError:
            pytest.skip("foreign uid requires real root, covered by privileged CI")
    else:
        (release / target.wheel_name).unlink()
        (release / "venv").mkdir()
    with pytest.raises(update.InstallError):
        update.recognize_current(target, preflight)
    assert release.exists() and current.exists()


def make_state(update, target, current):
    identity, _ = update.stored_release(current.name)
    return update.state_files.UpdateState(
        "prepared",
        current.name,
        identity["wheel_sha256"],
        identity["schema"],
        target.version,
        target.identity["wheel_sha256"],
        target.manifest_sha256,
        target.identity["schema"],
    )


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("after", [False, True])
def test_state_replace_interruption_does_not_invent_phase(
    managed, monkeypatch, existing, after
):
    update, target, current, _ = managed
    files = update.state_files
    state = make_state(update, target, current)
    if existing:
        files.write(state)
    original = os.replace

    def interrupted(source, destination):
        if after:
            original(source, destination)
        raise KeyboardInterrupt

    monkeypatch.setattr(files.os, "replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        files.write(replace(state, phase="migrating") if existing else state)
    expected = (
        replace(state, phase="migrating")
        if existing and after
        else state
        if existing or after
        else None
    )
    assert files.read() == expected
    files.clean_scratch()
    assert files.read() == expected
    assert not files.SCRATCH.exists()
    assert update.active_version() == current.name
    files.remove()
    assert files.read() is None


@pytest.mark.parametrize("path_name", ["STATE", "SCRATCH"])
@pytest.mark.parametrize("damage", ["mode", "symlink", "hardlink"])
def test_unsafe_state_metadata_preserved(managed, path_name, damage):
    update, target, current, _ = managed
    files = update.state_files
    path = getattr(files, path_name)
    if damage == "mode":
        path.write_text("{}")
        path.chmod(0o644)
    elif damage == "symlink":
        path.symlink_to(next(current.glob("*.whl")))
    else:
        os.link(next(current.glob("*.whl")), path)
    with pytest.raises(update.InstallError):
        files.write(make_state(update, target, current))
    assert os.path.lexists(path)


def test_persisted_exact_target_layout_requires_both_identities(managed):
    update, target, current, preflight = managed
    release = update.host.RELEASES / target.version
    release.mkdir()
    populate_release(release, target.version, target.wheel, target.requirements)
    state = make_state(update, target, current)
    update.state_files.write(state)
    assert update.recognize_current(target, preflight)[1] == state
    with pytest.raises(update.InstallError, match="identity_mismatch"):
        update.recognize_current(replace(target, manifest_sha256="d" * 64), preflight)
    assert current.exists() and release.exists()
