"""Linux CEC adapter authority and TV-only power messages."""

import re

from ._backend import Backend
from ._types import Kind, PowerError, Reason, State

DEVICE = re.compile(r"/dev/cec(?:0|[1-9][0-9]{0,3})\Z")


class CecBackend(Backend):
    def __init__(self, device: str | None = None):
        super().__init__(Kind.CEC)
        if device is not None and (
            not isinstance(device, str) or not DEVICE.fullmatch(device)
        ):
            raise ValueError("Invalid trusted CEC selector.")
        self._device = device

    def _operate(self, requested, cancelled):
        if self._device is None:
            raise PowerError(Reason.NOT_CONFIGURED)
        args = ["--device", self._device]
        info = self._command.run(args, {}, cancelled)
        # cec-ctl opens exactly the configured adapter and reports kernel caps.
        caps = re.findall(rb"(?m)^\s*Capabilities\s*: 0x([0-9a-fA-F]{8})\s*$", info)
        addresses = re.findall(
            rb"(?m)^\s*Logical Address\s*: ([0-9]{1,2}) \([^\r\n]+\)\s*$", info
        )
        if len(caps) != 1 or not int(caps[0], 16) & 8 or len(addresses) != 1:
            raise PowerError(Reason.UNAVAILABLE)
        address = int(addresses[0])
        if not 1 <= address <= 14:
            raise PowerError(Reason.UNAVAILABLE)
        args += [
            "--skip-info",
            "--show-raw",
            "--from",
            str(address),
            "--to",
            "0",
            "--timeout",
            "1000",
        ]
        if requested is not None:
            message = "--image-view-on" if requested == State.ON else "--standby"
            self._command.run([*args, message], {}, cancelled)
        raw = self._command.run([*args, "--give-device-power-status"], {}, cancelled)
        # Require the actual three-byte TV Report Power Status reply, not a
        # echoed request, a successful exit, or another adapter's traffic.
        replies = re.findall(
            rb"Received from TV \(0\):\s*REPORT_POWER_STATUS \(0x90\):\n"
            rb"[ \t]+power-status: [^\r\n]{1,80}\n"
            rb"[ \t]+Raw: 0x([0-9a-f]{2}) 0x90 0x([0-9a-f]{2}) \([^\r\n]{3}\)\n",
            raw,
        )
        if (
            raw.count(b"Received from ") != 1
            or len(replies) != 1
            or int(replies[0][0], 16) != address
        ):
            return State.UNKNOWN
        return {0: State.ON, 1: State.OFF}.get(int(replies[0][1], 16), State.UNKNOWN)
