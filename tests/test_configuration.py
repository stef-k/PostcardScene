"""The two process entrypoints share operator-owned filesystem authority."""

import pytest

from postcardscene.configuration import load_runtime_config
from postcardscene.filesystem_source import InvalidSource, PathPolicy
from postcardscene.runtime import cli
from postcardscene.web import create_app


def test_web_runtime_share_trusted_roots_and_database(tmp_path, monkeypatch):
    config = tmp_path / "operator.py"
    database = tmp_path / "state.sqlite3"
    root = tmp_path / "media"
    config.write_text(
        f"DATABASE_PATH = {str(database)!r}\n"
        f"MEDIA_ALLOWED_ROOTS = [{str(root)!r}]\n"
        "TRUSTED_HOSTS = ['localhost']\n"
    )
    monkeypatch.setenv("POSTCARDSCENE_CONFIG", str(config))
    runtime = load_runtime_config()
    app = create_app()
    assert runtime["DATABASE_PATH"] == app.config["DATABASE_PATH"] == str(database)
    assert (
        PathPolicy(runtime["MEDIA_ALLOWED_ROOTS"])
        == PathPolicy(app.config["MEDIA_ALLOWED_ROOTS"])
        == PathPolicy([root])
    )
    assert "TRUSTED_HOSTS" not in runtime
    assert runtime["PANEL_POWER_RUNTIME_ENABLED"] is False
    assert not database.exists()


def test_default_policy_has_no_authority(monkeypatch):
    monkeypatch.delenv("POSTCARDSCENE_CONFIG", raising=False)
    assert create_app().config["MEDIA_ALLOWED_ROOTS"] == ()
    assert load_runtime_config()["MEDIA_ALLOWED_ROOTS"] == ()


@pytest.mark.parametrize("contents", ["missing", "unreadable"])
def test_config_file_failure_is_not_ignored(tmp_path, monkeypatch, capsys, contents):
    config = tmp_path / "operator.py"
    if contents == "unreadable":
        config.write_text("DATABASE_PATH = '/private/state'\n")
        config.chmod(0)
    monkeypatch.setenv("POSTCARDSCENE_CONFIG", str(config))
    try:
        with pytest.raises(OSError):
            create_app()
        with pytest.raises(OSError):
            load_runtime_config()
        assert cli.main() == 1
        assert capsys.readouterr().err == "Runtime failed and cannot continue.\n"
    finally:
        if config.exists():
            config.chmod(0o600)


@pytest.mark.parametrize("roots", ["'/media'", "['relative']", "['/']"])
def test_runtime_rejects_invalid_roots_before_host_start(tmp_path, monkeypatch, roots):
    config = tmp_path / "operator.py"
    config.write_text(f"MEDIA_ALLOWED_ROOTS = {roots}\n")
    monkeypatch.setenv("POSTCARDSCENE_CONFIG", str(config))
    with pytest.raises(InvalidSource):
        PathPolicy(load_runtime_config()["MEDIA_ALLOWED_ROOTS"])
    monkeypatch.setattr(cli, "RuntimeHost", lambda *args: pytest.fail("host started"))
    assert cli.main() == 1


@pytest.mark.parametrize(
    "setting",
    [
        "PANEL_POWER_RUNTIME_ENABLED = 1",
        "PANEL_POWER_RUNTIME_ENABLED = 'true'",
        "CEC_DEVICE = '/dev/cec0'",
        "PANEL_POWER_RUNTIME_ENABLED = True\nCEC_DEVICE = '/dev/cec01'",
        "PANEL_POWER_RUNTIME_ENABLED = True\nDDC_DISPLAY = True",
        "PANEL_POWER_RUNTIME_ENABLED = True\nDDC_DISPLAY = 10000",
        "PANEL_POWER_RUNTIME_ENABLED = True\nDISPLAY_CONNECTOR = 'DP-1'",
    ],
)
def test_invalid_panel_configuration_fails_before_start(tmp_path, monkeypatch, setting):
    config = tmp_path / "operator.py"
    config.write_text(setting)
    monkeypatch.setenv("POSTCARDSCENE_CONFIG", str(config))
    with pytest.raises(ValueError):
        load_runtime_config()
    monkeypatch.setattr(cli, "RuntimeHost", lambda *args: pytest.fail("host started"))
    assert cli.main() == 1


def test_valid_panel_configuration(tmp_path, monkeypatch):
    config = tmp_path / "operator.py"
    config.write_text(
        "PANEL_POWER_RUNTIME_ENABLED = True\nCEC_DEVICE = '/dev/cec0'\n"
        "DDC_DISPLAY = 2\nDISPLAY_CONNECTOR = 'HDMI-A-1'\n"
    )
    monkeypatch.setenv("POSTCARDSCENE_CONFIG", str(config))
    result = load_runtime_config()
    assert result["PANEL_POWER_RUNTIME_ENABLED"] is True
    assert result["CEC_DEVICE"] == "/dev/cec0"
    assert result["DDC_DISPLAY"] == 2
    assert result["DISPLAY_CONNECTOR"] == "HDMI-A-1"
