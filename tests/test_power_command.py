"""Exercise actual pipes, deadlines and reaping without hardware or host tools."""

import subprocess
import sys
import time
from threading import Event, Timer

import pytest

import postcardscene.power._command as commands
from postcardscene.power import Kind, Reason
from postcardscene.power._command import Command
from postcardscene.power._types import PowerError


@pytest.mark.parametrize(
    "kind,tool",
    [
        (Kind.CEC, "/usr/bin/cec-ctl"),
        (Kind.DDC, "/usr/bin/ddcutil"),
        (Kind.SIGNAL, "/usr/bin/wlopm"),
    ],
)
def test_fixed_executable_and_explicit_environment(monkeypatch, kind, tool):
    popen = subprocess.Popen
    calls = []

    def launch(argv, **kwargs):
        calls.append((argv, kwargs))
        return popen([sys.executable, "-c", "print('bounded')"], **kwargs)

    monkeypatch.setattr(commands.subprocess, "Popen", launch)
    owner = Command(kind)
    assert (
        owner.run(["--test-argument"], {"WAYLAND_DISPLAY": "wayland-0"}, lambda: False)
        == b"bounded\n"
    )
    argv, kwargs = calls[0]
    assert argv == [tool, "--test-argument"]
    assert kwargs == {
        "env": {"WAYLAND_DISPLAY": "wayland-0", "LC_ALL": "C"},
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "shell": False,
    }
    assert owner.process is None and not owner.cleanup_failed


@pytest.mark.parametrize(
    "program,reason",
    [
        ("import time; time.sleep(10)", Reason.TIMEOUT),
        ("import os,time; os.close(1); time.sleep(10)", Reason.TIMEOUT),
        ("import os; os.write(1, b'x' * 20000)", Reason.OVERSIZED),
        ("import sys; print('PRIVATE'); sys.exit(1)", Reason.TOOL_FAILED),
    ],
)
def test_failed_children_are_bounded_sanitized_and_reaped(monkeypatch, program, reason):
    popen = subprocess.Popen
    children = []

    def launch(argv, **kwargs):
        child = popen([sys.executable, "-c", program], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(commands.subprocess, "Popen", launch)
    monkeypatch.setattr(commands, "COMMAND_TIMEOUT", 0.2)
    owner = Command(Kind.CEC)
    started = time.monotonic()
    with pytest.raises(PowerError) as caught:
        owner.run([], {}, lambda: False)
    assert caught.value.reason == reason and not caught.value.cleanup_failed
    assert "PRIVATE" not in str(caught.value)
    assert time.monotonic() - started < 1.5
    assert children[0].poll() is not None and children[0].stdout.closed
    assert owner.process is None


@pytest.mark.parametrize("close_stdout", [False, True])
def test_cancellation_interrupts_both_pipe_and_exit_wait(monkeypatch, close_stdout):
    popen = subprocess.Popen
    children = []
    cancelled = Event()

    def launch(argv, **kwargs):
        code = (
            "import os,time; "
            + ("os.close(1); " if close_stdout else "")
            + "time.sleep(10)"
        )
        child = popen([sys.executable, "-c", code], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(commands.subprocess, "Popen", launch)
    timer = Timer(0.1, cancelled.set)
    timer.start()
    try:
        with pytest.raises(PowerError) as caught:
            Command(Kind.DDC).run([], {}, cancelled.is_set)
        assert caught.value.reason == Reason.CANCELLED
        assert children[0].poll() is not None
    finally:
        timer.cancel()
        timer.join()


def test_unreapable_child_preserves_failure_and_blocks_replacement(monkeypatch):
    popen = subprocess.Popen
    child = popen(
        [sys.executable, "-c", "import time; time.sleep(10)"], stdout=subprocess.PIPE
    )
    real_wait = child.wait

    def cannot_reap(timeout):
        raise subprocess.TimeoutExpired("PRIVATE", timeout)

    monkeypatch.setattr(commands.subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(child, "wait", cannot_reap)
    monkeypatch.setattr(commands, "COMMAND_TIMEOUT", 0.1)
    owner = Command(Kind.CEC)
    try:
        with pytest.raises(PowerError) as caught:
            owner.run([], {}, lambda: False)
        assert caught.value.reason == Reason.TIMEOUT
        assert caught.value.cleanup_failed and owner.process is child
        with pytest.raises(PowerError) as repeated:
            owner.run([], {}, lambda: False)
        assert repeated.value.reason == Reason.CLEANUP_FAILED
    finally:
        child.kill()
        real_wait(timeout=2)
        child.stdout.close()


@pytest.mark.parametrize(
    "error,reason",
    [
        (FileNotFoundError("PRIVATE"), Reason.TOOL_MISSING),
        (PermissionError("PRIVATE"), Reason.TOOL_FAILED),
    ],
)
def test_launch_failure_is_sanitized(monkeypatch, error, reason):
    def launch(*args, **kwargs):
        raise error

    monkeypatch.setattr(commands.subprocess, "Popen", launch)
    with pytest.raises(PowerError) as caught:
        Command(Kind.SIGNAL).run([], {}, lambda: False)
    assert caught.value.reason == reason
    assert "PRIVATE" not in str(caught.value)
