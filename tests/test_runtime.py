import dataclasses
import selectors
import signal
import subprocess
import sys
from threading import Event, Thread

import pytest

from postcardscene.runtime import Lifecycle, RuntimeHost, cli


def test_lifecycle_and_prompt_cancellation(monkeypatch):
    host = RuntimeHost()
    initial = host.status
    states = [initial.state]
    waiting = Event()
    original_wait = host.stop_event.wait
    original_set = host._set_state

    def record(state):
        original_set(state)
        states.append(host.status.state)

    def wait():
        waiting.set()
        return original_wait()

    monkeypatch.setattr(host, "_set_state", record)
    monkeypatch.setattr(host.stop_event, "wait", wait)
    thread = Thread(target=host.run, daemon=True)
    thread.start()
    try:
        assert waiting.wait(2)
        assert host.status.state == Lifecycle.RUNNING
        host.request_shutdown()
        thread.join(2)
        assert not thread.is_alive()
        assert states == [
            Lifecycle.STARTING,
            Lifecycle.RUNNING,
            Lifecycle.STOPPING,
            Lifecycle.STOPPED,
        ]
        assert initial.state == Lifecycle.STARTING
        with pytest.raises(dataclasses.FrozenInstanceError):
            initial.summary = "changed"
    finally:
        host.request_shutdown()
        thread.join(2)


def test_degraded_remains_operational(monkeypatch):
    host = RuntimeHost()

    def optional_failure():
        host.mark_degraded()
        assert host.status.state == Lifecycle.DEGRADED
        assert not host.stop_event.is_set()
        host.request_shutdown()

    monkeypatch.setattr(host.stop_event, "wait", optional_failure)
    host.run()
    assert host.status.state == Lifecycle.STOPPED
    host.mark_degraded()
    assert host.status.state == Lifecycle.STOPPED


def test_fatal_failure_propagates_and_executable_returns_nonzero(monkeypatch, capsys):
    host = RuntimeHost()

    def fail():
        raise RuntimeError("secret /private/path")

    monkeypatch.setattr(host.stop_event, "wait", fail)
    with pytest.raises(RuntimeError, match="secret"):
        host.run()
    assert host.status.state == Lifecycle.ERROR
    assert host.stop_event.is_set()
    assert "secret" not in host.status.summary
    assert "/private" not in host.status.summary

    executable_host = RuntimeHost()
    monkeypatch.setattr(executable_host.stop_event, "wait", fail)
    monkeypatch.setattr(cli, "RuntimeHost", lambda: executable_host)
    previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    assert cli.main() == 1
    assert executable_host.status.state == Lifecycle.ERROR
    assert capsys.readouterr().err == "Runtime failed and cannot continue.\n"
    assert all(signal.getsignal(s) == handler for s, handler in previous.items())


def test_shutdown_before_run_and_no_restart():
    host = RuntimeHost()
    host.request_shutdown()
    host.request_shutdown()
    host.run()
    assert host.status.state == Lifecycle.STOPPED
    with pytest.raises(RuntimeError, match="only run once"):
        host.run()
    assert host.status.state == Lifecycle.STOPPED


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_packaged_entrypoint_without_flask_and_cooperative_signals(tmp_path, signum):
    # The wrapper announces readiness after executable handlers are installed.
    # It invokes the installed entry point, rejecting any Flask/web imports.
    script = """
import sys
from importlib.metadata import distribution
class RejectWeb:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "flask" or fullname.startswith(("flask.", "postcardscene.web")):
            raise AssertionError("Runtime must not import web code")
sys.meta_path.insert(0, RejectWeb())
from postcardscene.runtime import RuntimeHost, Lifecycle
original_run = RuntimeHost.run
def run(self):
    print("ready", flush=True)
    original_run(self)
    assert self.stop_event.is_set()
    assert self.status.state == Lifecycle.STOPPED
RuntimeHost.run = run
entry = next(e for e in distribution("postcardscene").entry_points
             if e.name == "postcardscene-runtime")
sys.exit(entry.load()())
"""
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=5), "Runtime did not become ready"
        assert process.stdout.readline() == "ready\n"
        process.send_signal(signum)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        assert stdout == stderr == ""
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()
