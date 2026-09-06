"""Run by provisioned system Python, never imported by the application.

ASCII lines on private inherited pipes. No socket, DOM, environment diagnostics,
raw input telemetry, full-screen interceptor or transport semantics.
"""

import os
import signal
import sys


def run():
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import Gdk, GLib, Gtk, GtkLayerShell

    if os.environ.get("GDK_BACKEND") != "wayland" or not GtkLayerShell.is_supported():
        return 1
    panel = Gtk.Window(title="PostcardScene probe")
    GtkLayerShell.init_for_window(panel)
    GtkLayerShell.set_layer(panel, GtkLayerShell.Layer.OVERLAY)
    GtkLayerShell.set_anchor(panel, GtkLayerShell.Edge.BOTTOM, True)
    GtkLayerShell.set_exclusive_zone(panel, 0)
    GtkLayerShell.set_keyboard_mode(panel, GtkLayerShell.KeyboardMode.NONE)
    button = Gtk.Button(label="Probe action")
    panel.add(button)
    # GTK's button handles pointer and emulated touch activation. No motion feed.
    button.connect("clicked", lambda *_: emit("probe_action"))
    panel.connect("delete-event", lambda *_: True)
    os.set_blocking(0, False)
    os.set_blocking(1, False)
    pending = bytearray()

    def emit(word):
        try:
            os.write(1, word.encode("ascii") + b"\n")
        except OSError:
            Gtk.main_quit()  # Parent gone or not consuming: no unbounded queue.

    def command(_fd, condition):
        if condition & (GLib.IO_HUP | GLib.IO_ERR):
            Gtk.main_quit()
            return False
        try:
            chunk = os.read(0, 256)
        except BlockingIOError:
            return True
        if not chunk or len(pending) + len(chunk) > 256:
            Gtk.main_quit()
            return False
        pending.extend(chunk)
        while b"\n" in pending:
            line, _, rest = pending.partition(b"\n")
            pending[:] = rest
            if line == b"show":
                panel.show_all()
                Gdk.Display.get_default().sync()
                emit("shown")
            elif line == b"hide":
                panel.hide()
                Gdk.Display.get_default().sync()
                emit("hidden")
            elif line == b"stop":
                Gtk.main_quit()
                return False
            else:
                Gtk.main_quit()
                return False
        return True

    GLib.io_add_watch(0, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, command)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, Gtk.main_quit)
    # Realize the layer-shell window without mapping visible controls.
    panel.realize()
    Gdk.Display.get_default().sync()
    emit("ready")
    Gtk.main()
    panel.destroy()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception:
        # Even direct helper invocation must not leak GI/platform diagnostics.
        sys.exit(1)
