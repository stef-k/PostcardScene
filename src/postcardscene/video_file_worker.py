"""One private Unix capability transfer per mounted video, with bounded cleanup."""

import array
import multiprocessing
import os
import select
import socket
import sys
import time

from postcardscene.filesystem_source import InvalidSource, SourceUnavailable
from postcardscene.mounted_source import (
    _check_open_mount,
    _cleanup_worker,
    _mounted_directory,
)
from postcardscene.video_file import (
    VideoFileError,
    VideoFileFailure,
    _check_cancelled,
    _open_selected,
)


def _video_worker(channel, selected, source, policy):
    try:
        _mounted_directory(source.configuration, policy)
        channel.send(b"progress")
        with _open_selected(
            selected, source, policy, lambda: channel.send(b"progress")
        ) as stream:
            _check_open_mount(stream)
            _mounted_directory(source.configuration, policy)
            channel.sendmsg(
                [b"ready"],
                [
                    (
                        socket.SOL_SOCKET,
                        socket.SCM_RIGHTS,
                        array.array("i", [stream.fileno()]),
                    )
                ],
            )
    except VideoFileError as error:
        _send_failure(channel, error.reason)
    except InvalidSource:
        _send_failure(channel, VideoFileFailure.INVALID)
    except (SourceUnavailable, OSError, RuntimeError):
        _send_failure(channel, VideoFileFailure.UNAVAILABLE)
    except Exception:
        _send_failure(channel, VideoFileFailure.HELPER)
    finally:
        channel.close()


def _send_failure(channel, reason):
    try:
        channel.send(reason.value.encode("ascii"))
    except OSError:
        pass


def _receive_message(channel):
    descriptors = array.array("i")
    try:
        message, ancillary, flags, _ = channel.recvmsg(
            32, socket.CMSG_SPACE(descriptors.itemsize), socket.MSG_CMSG_CLOEXEC
        )
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                descriptors.frombytes(
                    data[: len(data) - len(data) % descriptors.itemsize]
                )
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise VideoFileError(VideoFileFailure.HELPER)
        if message == b"ready" and len(descriptors) == 1:
            return message, descriptors.pop()
        if descriptors:
            raise VideoFileError(VideoFileFailure.HELPER)
        return message, None
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def _receive_fd(channel, cancelled, timeout):
    deadline = time.monotonic() + timeout
    while True:
        _check_cancelled(cancelled)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise VideoFileError(VideoFileFailure.TIMEOUT)
        if not select.select([channel], [], [], min(0.02, remaining))[0]:
            continue
        message, descriptor = _receive_message(channel)
        if descriptor is not None:
            return descriptor
        if message == b"progress":
            deadline = time.monotonic() + timeout
        elif message in {reason.value.encode("ascii") for reason in VideoFileFailure}:
            raise VideoFileError(VideoFileFailure(message.decode("ascii")))
        else:
            raise VideoFileError(VideoFileFailure.HELPER)


def pin_mounted_video(selected, source, policy, cancelled, timeout):
    """No mount/path/open/fstat work on the owner; no resource-sharer service."""
    try:
        receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    except OSError:
        raise VideoFileError(VideoFileFailure.HELPER) from None
    process = multiprocessing.get_context("spawn").Process(
        target=_video_worker, args=(sender, selected, source, policy)
    )
    descriptor = None
    try:
        try:
            try:
                process.start()
            except Exception:
                raise VideoFileError(VideoFileFailure.HELPER) from None
            sender.close()
            descriptor = _receive_fd(receiver, cancelled, timeout)
            _check_cancelled(cancelled)
        except (OSError, EOFError, RuntimeError):
            raise VideoFileError(VideoFileFailure.HELPER) from None
        finally:
            primary = sys.exception()
            receiver.close()
            sender.close()
            try:
                _cleanup_worker(process)
            except SourceUnavailable:
                error = VideoFileError(VideoFileFailure.HELPER)
                if primary is None:
                    raise error from None
                primary.add_note(str(error))
        return descriptor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
