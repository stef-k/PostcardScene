"""Owner-only compositor input carries only the closed playback vocabulary."""

import socket
from pathlib import Path
from xml.etree import ElementTree

import pytest

from postcardscene.graphics import WaylandSession
from postcardscene.graphics._capability import CapabilityError
from postcardscene.graphics.control_protocol import Action
from postcardscene.graphics.local_input import InputChannel, emit, probe_keybinding


@pytest.fixture
def channel(tmp_path):
    tmp_path.chmod(0o700)
    instance = InputChannel(WaylandSession(tmp_path))
    instance.start()
    yield instance
    instance.stop()


def test_private_input_delivery_and_cancellation(channel):
    assert channel.path.stat().st_mode & 0o777 == 0o600
    for action in Action:
        emit(channel.session, action.value)
        assert channel.receive() == action.value
    assert channel.receive(timeout_seconds=0.01) is None
    with pytest.raises(CapabilityError, match="cancelled"):
        channel.receive(cancelled=lambda: True)
    channel.stop()
    assert not channel.path.exists()
    with pytest.raises(CapabilityError, match="input_unavailable"):
        emit(channel.session, "activity")


def test_input_rejects_unknown_oversized_and_foreign_socket(channel):
    with pytest.raises(CapabilityError, match="invalid_event"):
        emit(channel.session, "raw_key")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
        sender.sendto(b"activity" + b"x" * 100, str(channel.path))
    with pytest.raises(CapabilityError, match="protocol_failed"):
        channel.receive()
    contender = InputChannel(channel.session)
    with pytest.raises(CapabilityError, match="input_unavailable"):
        contender.start()
    assert channel.path.exists()
    emit(channel.session, "activity")
    assert channel.receive() == "activity"


def test_invalid_authority_and_symlink_not_adopted(tmp_path):
    tmp_path.chmod(0o755)
    channel = InputChannel(WaylandSession(tmp_path))
    with pytest.raises(CapabilityError, match="input_unavailable"):
        channel.start()
    tmp_path.chmod(0o700)
    target = tmp_path / "foreign"
    target.write_text("keep")
    channel.path.symlink_to(target)
    with pytest.raises(CapabilityError, match="input_unavailable"):
        channel.start()
    channel.stop()
    assert target.read_text() == "keep" and channel.path.is_symlink()


def test_probe_binding_direct_fixed_helper_and_production_stays_inert():
    path = Path("/opt/postcardscene/venv/bin/postcardscene-input-emitter")
    binding = ElementTree.fromstring(probe_keybinding(path))
    assert binding.attrib == {"key": "W-F12"}
    assert binding.find("action").attrib == {"name": "Execute"}
    assert binding.findtext("action/command") == f"{path} activity"
    production = ElementTree.parse("src/postcardscene/graphics/labwc/rc.xml")
    assert all(
        action.get("name") == "None" for action in production.findall(".//action")
    )
    for unsafe in (
        Path("relative"),
        Path("/tmp/a;sh"),
        Path("/tmp/$HOME"),
        Path("/tmp/../bin/a"),
    ):
        with pytest.raises(CapabilityError, match="invalid_spec"):
            probe_keybinding(unsafe)
