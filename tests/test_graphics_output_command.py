"""Real bounded subprocess capture with a disposable local tool substitute."""

import subprocess
import sys
import time
from threading import Event, Timer

import pytest

from postcardscene.graphics import output_probe as probe


@pytest.mark.parametrize(
    "script, reason",
    [
        ("import sys; sys.stderr.write('SECRET'); sys.exit(1)", "tool_failed"),
        ("import time; time.sleep(5)", "tool_timeout"),
        ("import os, time; os.close(1); time.sleep(5)", "tool_timeout"),
        ("import sys; sys.stdout.write('x' * 300000)", "oversized_output"),
    ],
)
def test_bounded_capture_and_shell_free_session_environment(
    monkeypatch, script, reason
):
    popen = subprocess.Popen
    processes = []
    environment = {"WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/private"}

    def launch(argv, **kwargs):
        assert argv == ["/usr/bin/wlr-randr", "--json"]
        assert kwargs["env"] is environment
        assert kwargs["shell"] is False
        assert kwargs["stderr"] == subprocess.DEVNULL
        process = popen([sys.executable, "-c", script], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(probe.subprocess, "Popen", launch)
    monkeypatch.setattr(probe, "COMMAND_TIMEOUT", 0.2)
    started = time.monotonic()
    with pytest.raises(probe.ProbeError, match=reason) as error:
        probe.run_command(["--json"], environment)
    assert "SECRET" not in str(error.value)
    assert time.monotonic() - started < 1
    assert all(process.poll() is not None for process in processes)


def test_missing_tool_and_cancelled_command(monkeypatch):
    popen = subprocess.Popen

    def missing(*args, **kwargs):
        raise FileNotFoundError("SECRET")

    monkeypatch.setattr(probe.subprocess, "Popen", missing)
    with pytest.raises(probe.ProbeError, match="^tool_missing$"):
        probe.run_command(["--json"], {})
    processes = []

    def launch(argv, **kwargs):
        process = popen([sys.executable, "-c", "import time; time.sleep(5)"], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(probe.subprocess, "Popen", launch)
    stop = Event()
    timer = Timer(0.1, stop.set)
    timer.start()
    try:
        with pytest.raises(probe.ProbeError, match="^cancelled$"):
            probe.run_command(["--json"], {}, stop)
    finally:
        timer.join()
    assert processes[0].poll() is not None


@pytest.mark.parametrize("capture_fails", [False, True])
def test_cleanup_uncertainty_is_fatal_even_after_capture_failure(
    monkeypatch, capture_fails
):
    from io import BytesIO
    from types import SimpleNamespace

    def wait(timeout):
        raise subprocess.TimeoutExpired("private tool", timeout)

    process = SimpleNamespace(
        poll=lambda: None, kill=lambda: None, stdout=BytesIO(), wait=wait
    )
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *a, **kw: process)

    def capture(*args):
        if capture_fails:
            raise probe.ProbeError("tool_timeout")
        return b"[]"

    monkeypatch.setattr(probe, "_capture", capture)
    with pytest.raises(
        probe.ProbeCleanupError, match="^Output tool cleanup failed"
    ) as failure:
        probe.run_command([], {})
    if capture_fails:
        assert str(failure.value.__cause__.__context__) == "tool_timeout"
