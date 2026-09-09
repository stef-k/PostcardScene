"""Doctor contract: isolated failures, fixed output and unchanged installed data."""

import json
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from postcardscene.doctor import (
    CHECKS,
    Check,
    Report,
    bounded,
    cli,
    data,
    live,
    metadata,
)
from postcardscene.resource_health import GIB, MIB, evaluate_space
from postcardscene.schema import upgrade_database

SECRET = "private-password-cookie-https://private-host/media/secret.jpg"


def test_stable_human_json_and_exit_categories(monkeypatch):
    monkeypatch.setattr(cli, "inspect_bounded", lambda f, i: Check(i, "ready", "ready"))
    report = cli.collect()
    assert tuple(c.identifier for c in report.checks) == CHECKS
    assert report == cli.collect()
    assert report.exit_code == 0
    assert report.checks[-1] == Check("backup", "not_applicable", "not_implemented")
    assert json.loads(report.render(structured=True))["exit_code"] == 0
    assert "backup: not_applicable (not_implemented)" in report.render()
    assert Report((Check("graphics", "degraded", "no_display"),)).exit_code == 1
    assert Report((Check("release", "fatal", "identity_mismatch"),)).exit_code == 2


@pytest.mark.parametrize("failed", [{"web"}, {"web", "runtime", "graphics"}])
def test_service_failure_is_independent_and_sanitized(monkeypatch, failed):
    def query(unit):
        name = unit.split("-")[1].split(".")[0]
        return {
            "LoadState": "loaded",
            "UnitFileState": "enabled",
            "ActiveState": "failed" if name in failed else "active",
        }

    monkeypatch.setattr(live, "systemd_state", query)
    for name in ("graphics", "runtime", "web"):
        result = live.service("service_" + name)
        assert result.state == ("degraded" if name in failed else "ready")
        assert result.reason == ("service_failed" if name in failed else "ready")


def test_hung_and_crashing_checks_are_bounded_and_redacted(monkeypatch, capfd):
    monkeypatch.setattr(bounded, "CHECK_TIMEOUT", 0.15)

    def hung(identifier):
        print(SECRET, flush=True)
        time.sleep(30)

    started = time.monotonic()
    assert bounded.inspect_bounded(hung, "service_web").reason == "deadline"
    assert time.monotonic() - started < 2

    def failure(identifier):
        raise RuntimeError(SECRET)

    report = Report((bounded.inspect_bounded(failure, "service_web"),))
    assert report.checks[0].reason == "query_failed"
    assert (
        SECRET
        not in report.render() + report.render(structured=True) + capfd.readouterr().out
    )
    assert bounded.inspect_bounded(failure, "release").state == "fatal"


@pytest.mark.parametrize(
    "free,state",
    [(2 * GIB, "healthy"), (GIB - 1, "warning"), (256 * MIB - 1, "critical")],
)
def test_storage_uses_exact_owner_policy(monkeypatch, free, state):
    monkeypatch.setattr(
        live,
        "inspect_resource",
        lambda kind, root: evaluate_space(kind, 10 * GIB, free),
    )
    assert live.storage("storage_cache").details == (("space_state", state),)


@pytest.fixture
def diagnostic_db(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    upgrade_database(path)
    monkeypatch.setattr(data, "DATABASE", path)
    monkeypatch.setattr(data, "permissions", lambda identifier: None)
    monkeypatch.setattr(data, "configuration", lambda: {})
    return path


def installed_bytes(path):
    return {
        p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
        for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"))
        if p.exists()
    }


def test_database_checks_reuse_persistence_without_creating_original_sidecars(
    diagnostic_db, tmp_path
):
    before = installed_bytes(diagnostic_db)
    for index in range(2):
        scratch = tmp_path / str(index)
        scratch.mkdir()
        assert data.database_check("database", scratch) == Check(
            "database", "ready", "ready"
        )
        assert installed_bytes(diagnostic_db) == before


def test_live_wal_catalog_snapshot_includes_commits_and_leaves_originals_unchanged(
    diagnostic_db, tmp_path
):
    connection = sqlite3.connect(diagnostic_db)
    try:
        connection.execute(
            "INSERT INTO source (name, kind, configuration, enabled) VALUES (?, 'mounted_directory', ?, 1)",
            (SECRET, json.dumps({"path": SECRET})),
        )
        connection.commit()
        before = installed_bytes(diagnostic_db)
        scratch = tmp_path / "snapshot"
        scratch.mkdir()
        result = data.database_check("catalog", scratch)
        assert result.reason == "catalog_attention"
        assert dict(result.details) == {"sources": 1, "items": 0, "attention": 1}
        assert installed_bytes(diagnostic_db) == before
        assert SECRET not in Report((result,)).render(structured=True)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "damage,reason",
    [
        ("schema", "schema_incompatible"),
        ("wal", "integrity_failed"),
        ("foreign_key", "integrity_failed"),
        ("corrupt", "integrity_failed"),
    ],
)
def test_database_damage_uses_existing_check_categories(
    diagnostic_db, tmp_path, damage, reason
):
    if damage == "corrupt":
        diagnostic_db.write_bytes(b"corrupt " + SECRET.encode())
    else:
        with sqlite3.connect(diagnostic_db) as connection:
            if damage == "schema":
                connection.execute("UPDATE alembic_version SET version_num='unknown'")
            elif damage == "wal":
                connection.execute("PRAGMA journal_mode=DELETE")
            else:
                connection.execute(
                    "INSERT INTO media_catalog_state(source_id, scan_generation, completed_generation, last_result) VALUES (999, 0, 0, 'ready')"
                )
    before = installed_bytes(diagnostic_db)
    scratch = tmp_path / "snapshot"
    scratch.mkdir()
    result = data.database_check("database", scratch)
    assert result.reason == reason
    assert installed_bytes(diagnostic_db) == before
    assert SECRET not in Report((result,)).render(structured=True)


def test_snapshot_rejects_concurrent_change_and_size_limit(
    diagnostic_db, tmp_path, monkeypatch
):
    original = data.read_regular

    def changed(path, limit):
        value = original(path, limit)
        os.utime(path, ns=(1, 1))
        return value

    monkeypatch.setattr(data, "read_regular", changed)
    scratch = tmp_path / "snapshot"
    scratch.mkdir()
    assert data.database_check("database", scratch).reason == "database_unavailable"
    monkeypatch.setattr(data, "SNAPSHOT_LIMIT", 1)
    assert data.database_check("database", scratch).reason == "database_unavailable"


def test_config_never_executes_or_discloses_python(tmp_path, monkeypatch):
    config = tmp_path / "config.py"
    monkeypatch.setattr(data, "CONFIG", config)
    monkeypatch.setattr(data, "permissions", lambda identifier: None)
    marker = tmp_path / "executed"
    config.write_text(f"open({str(marker)!r}, 'w').write({SECRET!r})")
    assert data.web_config("web_config").reason == "config_not_inspectable"
    assert not marker.exists()
    config.write_text(f"TRUSTED_HOSTS = [{SECRET!r}]\n")
    report = Report((data.web_config("web_config"),))
    assert report.exit_code == 2
    assert SECRET not in report.render() + report.render(structured=True)
    config.write_text("TRUSTED_HOSTS = ['private-host']\n")
    assert data.web_config("web_config").reason == "direct_loopback"


def test_graphics_no_display_and_foreign_uid_do_not_reconcile(monkeypatch):
    monkeypatch.setattr(live, "identities", lambda: (os.geteuid(), 1234, 1235, 1236))
    monkeypatch.setattr(
        live,
        "WaylandSession",
        lambda: SimpleNamespace(inspect=lambda: SimpleNamespace(available=True)),
    )
    monkeypatch.setattr(live, "read_connectors", lambda: ())
    assert live.graphics("graphics") == Check("graphics", "degraded", "no_display")
    monkeypatch.setattr(
        live, "identities", lambda: (os.geteuid() + 1, 1234, 1235, 1236)
    )
    assert live.graphics("graphics").reason == "capability_unavailable"


def test_panel_tools_only_inspect_metadata(tmp_path, monkeypatch):
    tool = tmp_path / "tool"
    tool.write_text("must never execute")
    tool.chmod(0o755)
    monkeypatch.setattr(
        live, "identities", lambda: (os.geteuid(), 1234, os.getgid(), 1236)
    )
    monkeypatch.setattr(live.os, "getgrouplist", lambda *args: [os.getgid()])
    monkeypatch.setattr(live, "TOOLS", {kind: str(tool) for kind in live.Kind})
    for kind in live.Kind:
        assert live.panel_tool("panel_" + kind).reason == "software_ready"
    tool.chmod(0o644)
    assert live.panel_tool("panel_cec").reason == "tool_unavailable"


def test_metadata_rejects_wrong_mode_symlink_and_hardlink_without_read(tmp_path):
    key = tmp_path / "key"
    key.write_text(SECRET)
    key.chmod(0o600)
    metadata.metadata(key, os.getuid(), os.getgid(), 0o600)
    with pytest.raises(ValueError):
        metadata.metadata(key, os.getuid() + 1, os.getgid(), 0o600)
    with pytest.raises(ValueError):
        metadata.metadata(key, os.getuid(), os.getgid(), 0o640)
    link = tmp_path / "link"
    link.symlink_to(key)
    with pytest.raises(ValueError):
        metadata.metadata(link, os.getuid(), os.getgid(), 0o600)
    link.unlink()
    os.link(key, link)
    with pytest.raises(ValueError):
        metadata.metadata(key, os.getuid(), os.getgid(), 0o600)


@pytest.mark.parametrize(
    "script",
    [
        "import time; time.sleep(30)",
        "print('x' * 20000)",
        "print('LoadState=private-secret')",
    ],
)
def test_external_service_queries_bound_hangs_floods_and_unknown_values(
    monkeypatch, script
):
    import subprocess
    import sys

    original = subprocess.Popen

    def query(command, **kwargs):
        assert command[:3] == (
            "/usr/bin/systemctl",
            "show",
            "postcardscene-web.service",
        )
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        return original([sys.executable, "-I", "-c", script], **kwargs)

    monkeypatch.setattr(bounded.subprocess, "Popen", query)
    monkeypatch.setattr(bounded, "CHECK_TIMEOUT", 0.3)
    start = time.monotonic()
    result = bounded.inspect_bounded(live.service, "service_web")
    assert result.state == "unavailable"
    assert time.monotonic() - start < 2
    assert "private-secret" not in Report((result,)).render(structured=True)


def test_identity_separation_rejects_extra_groups_and_root(monkeypatch):
    accounts = {
        "postcardscene": SimpleNamespace(
            pw_uid=2001, pw_gid=2001, pw_name="postcardscene"
        ),
        "postcardscene-web": SimpleNamespace(
            pw_uid=2002, pw_gid=2001, pw_name="postcardscene-web"
        ),
    }
    monkeypatch.setattr(metadata.pwd, "getpwnam", accounts.__getitem__)
    monkeypatch.setattr(
        metadata.grp,
        "getgrnam",
        lambda name: SimpleNamespace(gr_gid=2001 if name == "postcardscene" else 2002),
    )
    monkeypatch.setattr(metadata.os, "getgrouplist", lambda *args: [2001])
    assert metadata.identities() == (2001, 2002, 2001, 2002)
    monkeypatch.setattr(metadata.os, "getgrouplist", lambda *args: [2001, 44])
    assert metadata.inspect("identities").state == "fatal"
    monkeypatch.setattr(metadata.os, "getgrouplist", lambda *args: [2001])
    accounts["postcardscene-web"].pw_uid = 0
    assert metadata.inspect("identities").state == "fatal"


def test_conflict_record_is_bounded_structural_data_never_output(monkeypatch):
    record = {
        unit: {
            "LoadState": "loaded",
            "ActiveState": "active",
            "UnitFileState": "enabled",
            "local_symlink": SECRET,
        }
        for unit in ("getty@tty1.service", "display-manager.service")
    }
    monkeypatch.setattr(metadata, "metadata", lambda *args: None)
    monkeypatch.setattr(
        metadata, "read_regular", lambda path, limit: json.dumps(record)
    )
    result = metadata.inspect("conflict_record")
    assert result.state == "ready"
    assert SECRET not in Report((result,)).render(structured=True)
    record["display-manager.service"]["ActiveState"] = SECRET
    assert metadata.inspect("conflict_record").state == "degraded"
    record["display-manager.service"]["ActiveState"] = [SECRET]
    assert metadata.inspect("conflict_record").state == "degraded"


def test_private_key_is_metadata_only_with_installer_primary_group(monkeypatch):
    seen = []
    monkeypatch.setattr(metadata, "identities", lambda: (2001, 2002, 2001, 2002))
    monkeypatch.setattr(metadata, "metadata", lambda *args: seen.append(args))
    monkeypatch.setattr(
        metadata, "read_regular", lambda *args: pytest.fail("Key bytes read")
    )
    metadata.permissions("private_key_permissions")
    assert seen[0][1:4] == (2002, 2002, 0o700)
    assert seen[1][1:] == (2002, 2001, 0o600)


def test_invalid_invocation_does_not_echo_private_arguments(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["postcardscene-doctor", "--config=" + SECRET])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert SECRET not in capsys.readouterr().err


def test_redirected_managed_config_is_fatal(tmp_path, monkeypatch):
    config = tmp_path / "config.py"
    config.write_text(f"DATABASE_PATH = {SECRET!r}\n")
    monkeypatch.setattr(data, "CONFIG", config)
    monkeypatch.setattr(data, "permissions", lambda identifier: None)
    assert data.web_config("web_config") == Check(
        "web_config", "fatal", "config_invalid"
    )
