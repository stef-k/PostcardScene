from configparser import ConfigParser
from importlib.resources import files

import pytest

from postcardscene.runtime import RuntimeHost, cli


def test_packaged_runtime_service_contract():
    assets = files("postcardscene.runtime").joinpath("systemd")
    assert [asset.name for asset in assets.iterdir()] == [
        "postcardscene-runtime.service"
    ]
    unit = ConfigParser(interpolation=None)
    unit.optionxform = str
    unit.read_string(assets.joinpath("postcardscene-runtime.service").read_text())
    assert {section: dict(unit[section]) for section in unit.sections()} == {
        "Unit": {
            "Description": "PostcardScene runtime",
            "After": "postcardscene-graphics.service",
            "Wants": "postcardscene-graphics.service",
            "StartLimitIntervalSec": "60",
            "StartLimitBurst": "5",
        },
        "Service": {
            "Type": "exec",
            "User": "postcardscene",
            "Group": "postcardscene",
            "Environment": "POSTCARDSCENE_CONFIG=/etc/postcardscene/config.py",
            "ExecStart": "/opt/postcardscene/venv/bin/postcardscene-runtime",
            "Restart": "on-failure",
            "RestartSec": "5",
            "TimeoutStartSec": "15",
            "TimeoutStopSec": "30",
            "KillMode": "control-group",
            "SendSIGKILL": "yes",
            "StandardOutput": "journal",
            "StandardError": "journal",
        },
        "Install": {"WantedBy": "multi-user.target"},
    }


@pytest.mark.parametrize(
    "failure", ["startup", "component_cleanup", "database_cleanup"]
)
def test_cli_failures_are_nonzero_and_sanitized(failure, catalog, monkeypatch, capsys):
    sensitive = (
        "secret /private/media https://private.invalid/token /dev/cec9 HDMI-A-99"
    )

    def fail(*args):
        raise RuntimeError(sensitive)

    database = catalog[0]
    host = RuntimeHost(database, catalog[3])
    host.request_shutdown()
    monkeypatch.setattr(cli, "Database", lambda path: database)
    monkeypatch.setattr(cli, "RuntimeHost", lambda *args: host)
    monkeypatch.setattr(
        cli,
        "load_runtime_config",
        lambda: {"DATABASE_PATH": database.path, "MEDIA_ALLOWED_ROOTS": []},
    )
    if failure == "startup":
        monkeypatch.setattr(cli, "load_runtime_config", fail)
    elif failure == "component_cleanup":
        original_join = host.catalog_worker.join

        def failed_join():
            original_join()
            fail()

        monkeypatch.setattr(host.catalog_worker, "join", failed_join)
    else:
        monkeypatch.setattr(database.engine, "dispose", fail)
    try:
        assert cli.main() == 1
    finally:
        monkeypatch.undo()
    captured = capsys.readouterr()
    assert captured.out == ""
    expected = "INFO Runtime startup beginning.\n"
    if failure != "startup":
        expected += (
            "INFO Runtime entering normal service operation.\n"
            "INFO Runtime shutdown beginning.\n"
        )
        assert not host.catalog_worker.thread.is_alive()
    assert captured.err == expected + "ERROR Runtime failed and cannot continue.\n"
