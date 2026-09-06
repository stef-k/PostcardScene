"""Real process/pipe failure and minimal native-window probe contracts."""

import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from postcardscene.graphics._capability import CapabilityError, Deadline, HelperProcess
from postcardscene.graphics.mpv_probe import PROBE_FLAGS, MpvSurfaceProbe
from postcardscene.graphics.overlay import Overlay


@pytest.fixture
def session():
    return SimpleNamespace(
        inspect=lambda: SimpleNamespace(available=True),
        client_environment=lambda: {
            "WAYLAND_DISPLAY": "wayland-0",
            "GDK_BACKEND": "wayland",
        },
    )


@pytest.fixture
def helper(tmp_path):
    script = tmp_path / "helper.py"
    script.write_text(
        "import sys\nprint('ready', flush=True)\n"
        "for line in sys.stdin:\n"
        " if line == 'show\\n': print('probe_action\\nshown', flush=True)\n"
        " elif line == 'hide\\n': print('hidden', flush=True)\n"
    )
    return script


@pytest.fixture
def overlay(session, helper, monkeypatch):
    import postcardscene.graphics.overlay as module

    actual = module.HelperProcess

    def launch(argv, environment):
        assert argv[0] == sys.executable
        assert argv[1].endswith("overlay_helper.py")
        assert environment == {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            **session.client_environment(),
        }
        return actual([sys.executable, str(helper)], environment)

    monkeypatch.setattr(module, "HelperProcess", launch)
    instance = Overlay(session, sys.executable)
    yield instance
    instance.stop()


def test_overlay_protocol_repeated_show_hide_action_and_cleanup(overlay):
    overlay.start()
    pid = overlay._process.child.pid
    overlay.start()
    assert overlay._process.child.pid == pid
    for _ in range(2):
        overlay.show()
        assert overlay.visible
        assert overlay.receive() == "probe_action"
        overlay.hide()
        assert not overlay.visible
    assert overlay.receive(timeout_seconds=0.02) is None
    overlay.stop()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not overlay.status.available


@pytest.mark.parametrize(
    "response, reason",
    [
        ("secret URL", "protocol_failed"),
        ("x" * 9000, "protocol_failed"),
        (None, "timeout"),
    ],
)
def test_overlay_bad_start_is_bounded_redacted_and_reaped(
    overlay, helper, response, reason
):
    helper.write_text(
        "import time\n"
        + (f"print({response!r}, flush=True)\n" if response else "")
        + "time.sleep(60)\n"
    )
    started = time.monotonic()
    with pytest.raises(CapabilityError, match=reason) as caught:
        overlay.start(timeout_seconds=0.1)
    assert str(caught.value) == reason
    assert time.monotonic() - started < 1.5
    assert overlay._process is None


def test_overlay_crash_cancel_and_unresponsive_command(overlay, helper):
    overlay.start()
    os.kill(overlay._process.child.pid, signal.SIGKILL)
    with pytest.raises(CapabilityError):
        overlay.show()
    assert overlay._process is None
    helper.write_text("import time\nprint('ready', flush=True)\ntime.sleep(60)\n")
    overlay.start()
    with pytest.raises(CapabilityError, match="timeout"):
        overlay.hide(timeout_seconds=0.05)
    with pytest.raises(CapabilityError, match="cancelled"):
        overlay.start(cancelled=lambda: True)
    assert overlay._process is None


TRACE = (
    " -> xdg_wm_base@4.get_xdg_surface(new id xdg_surface@9, wl_surface@8)\n"
    " -> xdg_surface@9.ack_configure(3)\n"
    " -> wl_surface@8.attach(wl_buffer@12, 0, 0)\n"
    " -> wl_surface@8.commit()\n"
)


def test_mpv_native_surface_readiness_and_group_cleanup(tmp_path, session, monkeypatch):
    import postcardscene.graphics.mpv_probe as module

    launcher = tmp_path / "mpv.py"
    launcher.write_text(
        f"import sys,time\nsys.stderr.write({TRACE!r})\nsys.stderr.flush()\ntime.sleep(60)\n"
    )
    actual = module.HelperProcess

    def launch(argv, environment, **kwargs):
        assert argv == [sys.executable, str(launcher), *PROBE_FLAGS]
        assert environment == {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "WAYLAND_DEBUG": "1",
            **session.client_environment(),
        }
        assert not any("ipc" in flag or "hwdec" in flag for flag in argv)
        return actual(argv, environment, **kwargs)

    monkeypatch.setattr(module, "HelperProcess", launch)
    probe = MpvSurfaceProbe(session, (sys.executable, str(launcher)))
    try:
        probe.ensure_started(timeout_seconds=1)
        assert probe.status.available
        pid = probe._process.child.pid
        probe.ensure_started()
        assert probe._process.child.pid == pid
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.02)
        with pytest.raises(CapabilityError, match="helper_exited"):
            probe.ensure_started()
        assert probe._process is None
    finally:
        probe.stop()


@pytest.mark.parametrize(
    "trace",
    [
        "",
        TRACE.replace("ack_configure", "configure"),
        TRACE.replace("wl_buffer@12", "nil"),
        TRACE.replace("wl_surface@8.commit", "wl_surface@99.commit"),
    ],
)
def test_mpv_process_alive_is_not_surface_ready(tmp_path, session, trace):
    script = tmp_path / "inert.py"
    script.write_text(
        f"import sys,time\nsys.stderr.write({trace!r})\nsys.stderr.flush()\ntime.sleep(60)\n"
    )
    probe = MpvSurfaceProbe(session, (sys.executable, str(script)))
    with pytest.raises(CapabilityError, match="timeout"):
        probe.ensure_started(timeout_seconds=0.1)
    assert probe._process is None


def test_pinned_group_kills_descendant_after_launcher_exits(tmp_path):
    pidfile = tmp_path / "pid"
    script = tmp_path / "fork.py"
    script.write_text(
        "import os,signal,time\n"
        "pid=os.fork()\n"
        "if pid:\n print('ready', flush=True)\n time.sleep(0.05)\n os._exit(0)\n"
        f"open({str(pidfile)!r},'w').write(str(os.getpid()))\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n"
    )
    process = HelperProcess([sys.executable, str(script)], {})
    try:
        assert process.line(Deadline(1)) == b"ready"
        time.sleep(0.1)
        assert process.exited()
        process.stop()
        child = int(pidfile.read_text())
        deadline = time.monotonic() + 1
        while Path(f"/proc/{child}/stat").exists():
            if Path(f"/proc/{child}/stat").read_text().split()[2] == "Z":
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        process.stop()


@pytest.mark.parametrize(
    "command", ["/usr/bin/mpv", (), ("relative",), ("/usr/bin/mpv", "--audio=yes")]
)
def test_invalid_probe_launcher(session, command):
    with pytest.raises(CapabilityError, match="invalid_spec"):
        MpvSurfaceProbe(session, command)


def test_failed_overlay_cleanup_preserves_primary_and_blocks_relaunch(
    overlay, helper, monkeypatch
):
    helper.write_text("import time\nprint('ready', flush=True)\ntime.sleep(60)\n")
    overlay.start()
    process = overlay._process

    def fail_stop():
        raise CapabilityError("cleanup_failed")

    with monkeypatch.context() as patch:
        patch.setattr(process, "stop", fail_stop)
        with pytest.raises(CapabilityError, match="timeout") as caught:
            overlay.show(timeout_seconds=0.05)
        assert caught.value.cleanup_failed
        assert overlay._process is process
        with pytest.raises(CapabilityError):
            overlay.start()
        assert overlay._process is process
    overlay.stop()
    assert overlay._process is None


def test_unavailable_session_and_missing_helper_are_typed(session):
    overlay = Overlay(session, "/missing-postcardscene-system-python")
    with pytest.raises(CapabilityError, match="startup_failed"):
        overlay.start()
    session.inspect = lambda: SimpleNamespace(available=False)
    with pytest.raises(CapabilityError, match="session_unavailable"):
        overlay.start()
    probe = MpvSurfaceProbe(session, ("/missing-mpv",))
    with pytest.raises(CapabilityError, match="session_unavailable"):
        probe.ensure_started()
    assert overlay._process is None and probe._process is None
