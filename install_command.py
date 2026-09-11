"""Bounded native subprocesses with the fixed managed identity/environment."""

import os
import pwd
import selectors
import signal
import subprocess
import time

from postcardscene_install_services import InstallError

ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
    "LANG": "C",
    "HOME": "/root",
}


def command(
    args, *, interactive=False, user=None, timeout=300, capture=False, pass_fds=()
):
    if interactive and (capture or pass_fds):
        raise ValueError("Interactive commands cannot capture or inherit descriptors.")
    options = {}
    environment = dict(ENV)
    if user:
        account = pwd.getpwnam(user)
        options.update(user=account.pw_uid, group=account.pw_gid, extra_groups=[])
        environment.update(
            HOME=account.pw_dir, POSTCARDSCENE_CONFIG="/etc/postcardscene/config.py"
        )
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(
        args,
        env=environment,
        cwd="/",
        umask=0o007 if user else 0o022,
        stdin=None if interactive else subprocess.DEVNULL,
        stdout=subprocess.PIPE
        if capture
        else None
        if interactive
        else subprocess.DEVNULL,
        stderr=None if interactive else subprocess.DEVNULL,
        start_new_session=not interactive,
        pass_fds=pass_fds,
        **options,
    )
    try:
        output = capture_output(process, timeout) if capture else None
        status = process.wait(timeout=max(0, deadline - time.monotonic()))
        if status:
            raise InstallError("command_failed")
        return output
    except BaseException as error:
        try:
            if interactive:
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired as cleanup:
            error.add_note(
                "Command cleanup uncertain; inherited mutation lock may remain held."
            )
            raise error from cleanup
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()


def capture_output(process, timeout):
    """Bound installer helper results while retaining the normal managed runner."""
    deadline = time.monotonic() + timeout
    output = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InstallError("command_timeout")
            if not selector.select(min(remaining, 0.1)):
                continue
            block = os.read(process.stdout.fileno(), 65536)
            if not block:
                return bytes(output)
            output.extend(block)
            if len(output) > 65536:
                raise InstallError("command_output_invalid")
