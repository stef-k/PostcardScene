"""System-Python GTK3 layer-shell controls; no application secrets or GI in uv."""

import os
import signal
import sys
from dataclasses import replace

if __package__:
    from .control_protocol import Action, ControlState, buttons, decode_state
else:
    from control_protocol import Action, ControlState, buttons, decode_state


class Panel:
    def __init__(self, Gtk, Gdk, layer, emit):
        self.Gtk = Gtk
        self.emit = emit
        self.state = ControlState()
        self.panel = self.window(Gtk, layer, "PostcardScene controls")
        self.panel.set_name("psc-controls")
        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.box.set_border_width(12)
        self.panel.add(self.box)
        self.controls = {}
        # Build once so a state update cannot destroy a button during a click.
        for action, label in buttons(ControlState(seekable=True, audio=True)):
            button = Gtk.Button(label=label)
            button.set_no_show_all(True)
            button.connect("clicked", lambda _button, event=action: emit(event.value))
            self.box.pack_start(button, False, False, 0)
            self.controls[action] = button
        self.hotspot = self.window(Gtk, layer, "PostcardScene edge reveal")
        self.hotspot.set_name("psc-edge")
        layer.set_anchor(self.hotspot, layer.Edge.LEFT, True)
        layer.set_anchor(self.hotspot, layer.Edge.RIGHT, True)
        self.hotspot.set_size_request(1, 8)
        self.hotspot.add_events(
            Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.TOUCH_MASK
        )
        for event in ("enter-notify-event", "button-press-event", "touch-event"):
            self.hotspot.connect(event, self.reveal)
        for window in (self.panel, self.hotspot):
            window.set_app_paintable(True)
            visual = window.get_screen().get_rgba_visual()
            if visual is None:
                raise RuntimeError("unavailable")
            window.set_visual(visual)
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            #psc-edge { background-color: transparent; background-image: none; }
            #psc-controls { background: rgba(20, 25, 32, 0.94); color: #f5f7fa;
                            border: 1px solid #687481; border-radius: 8px; }
            #psc-controls button { background: #293440; color: #f5f7fa;
                                   padding: 10px; border: 1px solid #687481; }
            #psc-controls button:focus { border: 2px solid #a9d9ff; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            self.panel.get_screen(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.panel.realize()
        self.hotspot.realize()

    @staticmethod
    def window(Gtk, layer, title):
        window = Gtk.Window(title=title)
        layer.init_for_window(window)
        layer.set_layer(window, layer.Layer.OVERLAY)
        layer.set_anchor(window, layer.Edge.BOTTOM, True)
        layer.set_exclusive_zone(window, 0)
        layer.set_keyboard_mode(window, layer.KeyboardMode.NONE)
        window.connect("delete-event", lambda *_: True)
        return window

    def reveal(self, *_):
        if self.state.enabled and not self.state.visible:
            self.emit(Action.ACTIVITY.value)
        return True

    def update(self, state):
        self.state = state
        labels = dict(buttons(state))
        for action, button in self.controls.items():
            button.set_visible(action in labels)
            if action in labels:
                button.set_label(labels[action])
        # Retire the old input surface before mapping its replacement.
        self.hotspot.hide()
        if state.visible:
            self.panel.show_all()
        else:
            self.panel.hide()
            if state.enabled:
                self.hotspot.show_all()

    def destroy(self):
        self.panel.destroy()
        self.hotspot.destroy()


class Commands:
    """Bounded stream framing; tested without loading GI."""

    def __init__(self, panel, sync, emit):
        self.panel, self.sync, self.emit = panel, sync, emit
        self.pending = bytearray()

    def feed(self, chunk):
        if not chunk:
            return False
        self.pending.extend(chunk)
        while b"\n" in self.pending:
            line, _, rest = self.pending.partition(b"\n")
            self.pending[:] = rest
            if len(line) + 1 > 1024:
                return False
            if line == b"stop":
                return False
            if line in (b"show", b"hide"):
                state = replace(self.panel.state, enabled=True, visible=line == b"show")
                reply = "shown" if state.visible else "hidden"
            else:
                try:
                    state = decode_state(line)
                except ValueError:
                    return False
                reply = "updated"
            self.panel.update(state)
            self.sync()
            self.emit(reply)
        return len(self.pending) < 1024


def run():
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import Gdk, GLib, Gtk, GtkLayerShell

    if os.environ.get("GDK_BACKEND") != "wayland" or not GtkLayerShell.is_supported():
        return 1
    os.set_blocking(0, False)
    os.set_blocking(1, False)

    def emit(word):
        try:
            data = word.encode("ascii") + b"\n"
            if os.write(1, data) != len(data):
                Gtk.main_quit()
        except OSError:
            Gtk.main_quit()

    panel = Panel(Gtk, Gdk, GtkLayerShell, emit)
    sync = Gdk.Display.get_default().sync
    commands = Commands(panel, sync, emit)

    def command(_fd, condition):
        if condition & (GLib.IO_HUP | GLib.IO_ERR):
            Gtk.main_quit()
            return False
        try:
            chunk = os.read(0, 1024)
        except BlockingIOError:
            return True
        if not commands.feed(chunk):
            Gtk.main_quit()
            return False
        return True

    GLib.io_add_watch(0, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, command)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, Gtk.main_quit)
    sync()
    emit("ready")
    try:
        Gtk.main()
    finally:
        panel.destroy()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception:
        sys.exit(1)  # No raw toolkit/platform diagnostics, even on direct invocation.
