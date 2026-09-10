"""Exact auxiliary unit authority, activation and failure-recovery contracts."""

import configparser
from types import SimpleNamespace

import pytest
from test_install_preflight import FixtureHost, pf
from test_native_install import ROOT, host, installer, services
from test_native_install import bundle as bundle


def test_packaged_auxiliary_units_preserve_three_services():
    assert host.SERVICES == tuple(
        f"postcardscene-{name}.service" for name in ("graphics", "runtime", "web")
    )
    assert host.AUXILIARY_UNITS == (
        "postcardscene-backup.service",
        "postcardscene-backup.timer",
    )
    service = configparser.ConfigParser(interpolation=None)
    service.read(ROOT / "src/postcardscene/backup/systemd/postcardscene-backup.service")
    values = service["Service"]
    assert values["Type"] == "oneshot"
    assert values["User"] == "postcardscene-web" and values["Group"] == "postcardscene"
    assert values["UMask"] == "0077"
    assert (
        values["ExecStart"]
        == "/opt/postcardscene/venv/bin/postcardscene-backup scheduled"
    )
    assert 620 <= int(values["TimeoutStartSec"]) <= 700
    assert values["NoNewPrivileges"] == "true"
    assert values["PrivateDevices"] == "yes" and values["DevicePolicy"] == "closed"
    assert values["CapabilityBoundingSet"] == values["AmbientCapabilities"] == ""
    assert not any(
        k in values for k in ("ReadWritePaths", "ProtectSystem", "ProtectHome")
    )
    timer = configparser.ConfigParser()
    timer.read(ROOT / "src/postcardscene/backup/systemd/postcardscene-backup.timer")
    assert timer["Timer"]["OnCalendar"] == "hourly"
    assert timer["Timer"].getboolean("Persistent")
    assert timer["Install"]["WantedBy"] == "timers.target"


@pytest.mark.parametrize(
    "unit", (*host.AUXILIARY_UNITS, "postcardscene-backup-extra.timer")
)
def test_clean_preflight_rejects_existing_auxiliary_or_foreign(unit):
    observer = FixtureHost()
    original = observer.command

    def command(args):
        if args[1] == "list-unit-files":
            return 0, f"{unit} enabled\n"
        return original(args)

    observer.command = command
    assert pf.preflight(observer).reasons == ("existing_service_unrecognized",)


@pytest.mark.parametrize(
    "damage", [None, "dropin", "foreign", "timer_disabled", "oneshot_enabled"]
)
def test_exact_auxiliary_authority_allows_inactive_oneshot(damage):
    def command(args):
        unit = args[2]
        dropins = (
            f"/etc/systemd/system/{unit}.d/permissions.conf"
            if unit in host.SERVICES[1:]
            else ""
        )
        if unit in host.AUXILIARY_UNITS and damage == "dropin":
            dropins = "/run/systemd/system/foreign.conf"
        return 0, f"FragmentPath=/etc/systemd/system/{unit}\nDropInPaths={dropins}\n"

    def state(_, unit):
        timer = unit.endswith(".timer")
        return {
            "LoadState": "loaded",
            "ActiveState": "active" if timer else "inactive",
            "UnitFileState": ("disabled" if damage == "timer_disabled" else "enabled")
            if timer
            else ("enabled" if damage == "oneshot_enabled" else "static"),
        }

    preflight = SimpleNamespace(
        Host=lambda: SimpleNamespace(command=command),
        Rejected=pf.Rejected,
        unit_names=lambda _: (
            set((*host.SERVICES, *host.AUXILIARY_UNITS))
            | ({"postcardscene-other.service"} if damage == "foreign" else set())
        ),
        service_state=state,
    )
    if damage:
        with pytest.raises(
            host.InstallError, match="managed_service_authority_invalid"
        ):
            host.service_authority(preflight)
            host.require_auxiliary_installed(preflight)
    else:
        host.service_authority(preflight)
        host.require_auxiliary_installed(preflight)


@pytest.mark.parametrize("reinstall", [False, True])
@pytest.mark.parametrize(
    "failure", ["timer_enabled", "timer_started", "later_activation", None]
)
def test_install_recovery_retires_auxiliary_work(
    bundle, monkeypatch, reinstall, failure
):
    inputs = (*installer.validate_inputs(bundle)[:-1], host)
    preflight = inputs[-2]
    monkeypatch.setattr(installer, "validate_inputs", lambda _: inputs)
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    classifications = iter(
        ["removed_preserved", "removed_preserved", "installed_managed"]
        if reinstall
        else ["clean"]
    )
    monkeypatch.setattr(host, "classify", lambda *a: next(classifications))
    preflight.preflight = lambda **kw: SimpleNamespace(
        ok=True, plan=SimpleNamespace(tools=())
    )
    events = []
    for name in (
        "install_packages",
        "directory",
        "write_new",
        "install_assets",
        "reserve_graphics",
        "require_stopped",
        "require_no_processes",
        "activate_payload",
    ):
        monkeypatch.setattr(host, name, lambda *a, **kw: None)
    monkeypatch.setattr(host, "stage_payload", lambda *a: "/staged/python")
    monkeypatch.setattr(host, "provision_identities", lambda *a: (11, 12, 13, 14))
    monkeypatch.setattr(host, "preserved_authority", lambda: (11, 12, 13, 14))
    monkeypatch.setattr(host, "preserved_identities", lambda: (11, 12, 13, 14))
    monkeypatch.setattr(host, "bootstrap", lambda *a: events.append("schema_ready"))
    monkeypatch.setattr(
        installer,
        "validate_preserved_application",
        lambda *a: events.append("schema_ready"),
    )
    enabled = set()
    active = set()
    timer = host.AUXILIARY_UNITS[1]
    fired = False

    def run(args):
        nonlocal fired
        action = args[1]
        units = args[2:]
        events.append((action, units))
        if action == "enable":
            enabled.update(units)
        if action == "start":
            active.update(units)
        if action == "stop":
            active.difference_update(units)
        if action == "disable":
            enabled.difference_update(units)
        point = (
            "timer_enabled"
            if action == "enable" and units == (timer,)
            else "timer_started"
            if action == "start" and units == (timer,)
            else "later_activation"
            if action == "is-active" and timer in active and host.SERVICES[0] in units
            else None
        )
        if point:
            assert "schema_ready" in events
        if failure is not None and point == failure and not fired:
            fired = True
            # Model a timer-dispatched live oneshot during activation failure.
            active.add(host.AUXILIARY_UNITS[0])
            raise host.InstallError("injected_activation_failure")

    operation = installer.Installation(bundle, run)
    if failure:
        with pytest.raises(installer.InstallError):
            operation.install()
        operation.recover()
        assert not active and not enabled
        stop_timer = events.index(("stop", (timer,)))
        stop_services = events.index(("stop", host.SERVICES))
        assert stop_timer < stop_services
    else:
        operation.install()
        assert timer in active and timer in enabled
        assert host.AUXILIARY_UNITS[0] not in active


def test_auxiliary_recovery_attempts_all_actions_after_stop_failure():
    calls = []

    def run(args):
        calls.append(args[1:])
        if len(calls) == 1:
            raise OSError("failure")

    with pytest.raises(services.InstallError, match="auxiliary_stop_failed"):
        services.stop_auxiliary(run)
    assert calls == [
        ("stop", host.AUXILIARY_UNITS[1]),
        ("disable", host.AUXILIARY_UNITS[1]),
        ("stop", host.AUXILIARY_UNITS[0]),
    ]


def test_executing_oneshot_is_recognized_for_removal():
    observer = SimpleNamespace(
        command=lambda _: (
            0,
            "LoadState=loaded\nActiveState=activating\nUnitFileState=static\n",
        )
    )
    assert (
        pf.service_state(observer, host.AUXILIARY_UNITS[0])["ActiveState"]
        == "activating"
    )
    with pytest.raises(pf.Rejected):
        pf.service_state(observer, host.SERVICES[0])


def test_removed_auxiliary_cannot_retain_enablement():
    observer = FixtureHost()
    original = observer.command

    def command(args):
        if args[1] == "show" and args[2] == host.AUXILIARY_UNITS[1]:
            return (
                0,
                "LoadState=not-found\nActiveState=inactive\nUnitFileState=enabled\n",
            )
        return original(args)

    observer.command = command
    with pytest.raises(pf.Rejected, match="existing_service_unrecognized"):
        pf.service_check(observer, None, "preserved")
