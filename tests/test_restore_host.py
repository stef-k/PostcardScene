"""Fixed unit authority and persistent retirement, without a systemd daemon."""

import pytest

from postcardscene.backup import cli
from postcardscene.backup import restore_host as host
from postcardscene.backup.files import BackupError


def test_cli_root_required(monkeypatch, capsys):
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    assert cli.main(["restore", "/private/archive"]) == 1
    assert capsys.readouterr().out == '{"error": "restore_root_required"}\n'


def test_retirement_attempts_every_unit_after_failure(monkeypatch):
    calls = []

    def systemctl(*args):
        calls.append(args)
        if args == ("stop", host.AUXILIARY_UNITS[1]):
            raise OSError("private")

    monkeypatch.setattr(host, "systemctl", systemctl)
    monkeypatch.setattr(
        host,
        "state",
        lambda unit: {
            "ActiveState": "inactive",
            "UnitFileState": "static"
            if unit == host.AUXILIARY_UNITS[0]
            else "disabled",
        },
    )
    monkeypatch.setattr(
        host, "require_quiescent", lambda ids: calls.append(("quiescent",))
    )
    with pytest.raises(BackupError, match="restore_stop_failed"):
        host.stop((1, 2, 3, 4))
    assert calls[:3] == [
        ("stop", host.AUXILIARY_UNITS[1]),
        ("disable", host.AUXILIARY_UNITS[1]),
        ("stop", host.AUXILIARY_UNITS[0]),
    ]
    for unit in host.SERVICES:
        assert ("stop", unit) in calls and ("disable", unit) in calls
    assert calls[-1] == ("quiescent",)


def test_foreign_unit_rejected(monkeypatch):
    monkeypatch.setattr(
        host, "systemctl", lambda *args: "postcardscene-foreign.service enabled\n"
    )
    with pytest.raises(BackupError, match="restore_host_invalid"):
        host.unit_authority()


def test_timer_activation_unconditional(monkeypatch):
    calls = []
    monkeypatch.setattr(host, "systemctl", lambda *args: calls.append(args))
    monkeypatch.setattr(
        host,
        "state",
        lambda unit: {
            "LoadState": "loaded",
            "ActiveState": "active",
            "UnitFileState": "enabled",
        },
    )
    host.activate_services()
    assert calls[-1] == ("enable", host.AUXILIARY_UNITS[1])
    assert ("start", host.AUXILIARY_UNITS[1]) not in calls
    host.activate_timer()
    assert calls[-1] == ("start", host.AUXILIARY_UNITS[1])
