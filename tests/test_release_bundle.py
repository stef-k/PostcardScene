"""Published-byte checksum, fixed manifest and safe native extraction contracts."""

import hashlib
import io
import json
import tarfile

import pytest
from test_native_install import bundle as bundle
from test_native_install import installer, release


@pytest.mark.parametrize("action", ["install", "remove", "update"])
@pytest.mark.parametrize(
    "damage",
    ["missing", "extra", "tag", "schema", "version", "python", "hash", "duplicate"],
)
def test_manifest_failure_precedes_host_support_and_mutation(
    bundle, monkeypatch, action, damage
):
    path = bundle / "release-manifest.json"
    manifest = json.loads(path.read_text())
    if damage == "missing":
        path.unlink()
    elif damage == "extra":
        (bundle / "foreign").write_text("foreign")
    elif damage == "duplicate":
        path.write_text(
            path.read_text().replace(
                '"manifest_schema":1', '"manifest_schema":1,"manifest_schema":1'
            )
        )
    else:
        if damage == "tag":
            manifest["tag"] = "v9.9.9"
        elif damage == "schema":
            manifest["schema"]["alembic_head"] = "unknown"
        elif damage == "version":
            manifest["version"] = "9.9.9"
        elif damage == "python":
            manifest["requires_python"] = ">=3.15"
        else:
            manifest["members"]["install.py"]["sha256"] = "0" * 64
        path.write_text(json.dumps(manifest))
    original = installer.importlib.util.module_from_spec

    def only_validator(spec):
        assert spec.name == "postcardscene_install_inputs", (
            "Host support executed before manifest validation"
        )
        return original(spec)

    monkeypatch.setattr(installer.importlib.util, "module_from_spec", only_validator)
    operation = installer.Installation(
        bundle, lambda *a, **kw: pytest.fail("host mutation")
    )
    with pytest.raises((installer.InstallError, OSError)):
        getattr(operation, action)()
    assert operation.host is None and operation.phase == "validation"


def test_manifest_does_not_replace_code_owned_pins(bundle, monkeypatch):
    helper = bundle / "install_host.py"
    helper.write_text("raise AssertionError('untrusted code executed')")
    release.write_manifest(bundle, "0.1.0.dev0", "a" * 40)
    monkeypatch.setattr(
        installer.importlib.util,
        "module_from_spec",
        lambda _: pytest.fail("helper executed"),
    )
    with pytest.raises(installer.InstallError, match="install_support_mismatch"):
        installer.validate_inputs(bundle)


@pytest.mark.parametrize("name", ["install.py", "release-manifest.json"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_manifest_inputs_reject_links(bundle, tmp_path, name, kind):
    path = bundle / name
    saved = tmp_path / "saved"
    path.rename(saved)
    if kind == "symlink":
        path.symlink_to(saved)
    else:
        path.hardlink_to(saved)
    with pytest.raises((installer.InstallError, OSError)):
        installer.validate_inputs(bundle)


def test_archive_roundtrip_is_deterministic_and_checksum_bound(bundle, tmp_path):
    first, second = tmp_path / "first.tar.gz", tmp_path / "second.tar.gz"
    release.write_archive(bundle, first, 1700000000)
    release.write_archive(bundle, second, 1700000000)
    assert first.read_bytes() == second.read_bytes()
    release.write_checksums(tmp_path, (first.name,))
    release.verify_checksums(tmp_path, (first.name,))
    extracted = tmp_path / "extracted"
    release.extract_archive(first, extracted, "0.1.0.dev0")
    assert installer.validate_inputs(extracted)[0] == "0.1.0.dev0"
    assert {p.name: p.read_bytes() for p in extracted.iterdir()} == {
        p.name: p.read_bytes() for p in bundle.iterdir()
    }
    first.write_bytes(first.read_bytes() + b"repacked")
    with pytest.raises(ValueError, match="checksum"):
        release.verify_checksums(tmp_path, (first.name,))


@pytest.mark.parametrize(
    "damage",
    [
        "extra",
        "duplicate",
        "absolute",
        "traversal",
        "symlink",
        "hardlink",
        "device",
        "mode",
        "owner",
        "pax",
        "missing",
    ],
)
def test_archive_rejects_unsafe_members_before_extraction(bundle, tmp_path, damage):
    path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for member in sorted(bundle.iterdir()):
            if damage == "missing" and member.name == "install.py":
                continue
            data = member.read_bytes()
            entry = tarfile.TarInfo(member.name)
            entry.mode = 0o644
            entry.size = len(data)
            if member.name == "install.py":
                if damage == "absolute":
                    entry.name = "/install.py"
                elif damage == "traversal":
                    entry.name = "../install.py"
                elif damage in ("symlink", "hardlink", "device"):
                    entry.type = {
                        "symlink": tarfile.SYMTYPE,
                        "hardlink": tarfile.LNKTYPE,
                        "device": tarfile.CHRTYPE,
                    }[damage]
                    entry.size = 0
                elif damage == "mode":
                    entry.mode = 0o4755
                elif damage == "owner":
                    entry.uid = 1000
                elif damage == "pax":
                    entry.pax_headers = {"comment": "unexpected"}
            archive.addfile(entry, io.BytesIO(data))
        if damage in ("extra", "duplicate"):
            entry = tarfile.TarInfo("foreign" if damage == "extra" else "install.py")
            entry.mode = 0o644
            archive.addfile(entry, io.BytesIO())
    extracted = tmp_path / "extracted"
    with pytest.raises(ValueError):
        release.extract_archive(path, extracted, "0.1.0.dev0")
    assert not extracted.exists()


def test_wheel_substitution_rejected_by_member_hash(bundle):
    wheel = next(bundle.glob("*.whl"))
    original = hashlib.sha256(wheel.read_bytes()).hexdigest()
    wheel.write_bytes(wheel.read_bytes() + b"substitution")
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() != original
    with pytest.raises(installer.InstallError, match="release_member_mismatch"):
        installer.validate_inputs(bundle)


@pytest.mark.parametrize("damage", [None, "dirty", "tag", "source", "event"])
def test_publication_requires_clean_exact_tag_identity(monkeypatch, damage):
    import importlib
    from types import SimpleNamespace

    monkeypatch.syspath_prepend(str(release.ROOT / "scripts"))
    builder = importlib.import_module("build_release")
    sha = "a" * 40
    monkeypatch.setenv(
        "GITHUB_REF", "refs/tags/v0.1.0.dev0" if damage != "tag" else "refs/tags/v9.9.9"
    )
    monkeypatch.setenv("GITHUB_SHA", sha if damage != "event" else "b" * 40)
    outputs = iter(
        [
            sha,
            b"1700000000",
            b" M install.py" if damage == "dirty" else b"",
            sha if damage != "source" else "b" * 40,
        ]
    )
    monkeypatch.setattr(
        builder, "run", lambda *a, **kw: SimpleNamespace(stdout=next(outputs))
    )
    if damage:
        with pytest.raises(ValueError):
            builder.source_identity({"version": "0.1.0.dev0"}, require_tag=True)
    else:
        assert builder.source_identity({"version": "0.1.0.dev0"}, require_tag=True) == (
            sha,
            1700000000,
        )


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.1.0.dev0", "true"),
        ("0.1.0rc1", "true"),
        ("0.1.0", "false"),
        ("0.1.0.post1", "false"),
    ],
)
def test_github_prerelease_follows_package_version(monkeypatch, version, expected):
    import importlib

    monkeypatch.syspath_prepend(str(release.ROOT / "scripts"))
    builder = importlib.import_module("build_release")
    assert builder.github_prerelease(version) == expected
