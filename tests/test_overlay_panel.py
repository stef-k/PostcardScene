"""Exercise native panel wiring with a small toolkit double, without importing GI."""

from types import SimpleNamespace

from postcardscene.graphics.control_protocol import Action, ControlState
from postcardscene.graphics.overlay_helper import Panel


class Widget:
    def __init__(self, **kwargs):
        self.label = kwargs.get("label")
        self.visible = False
        self.no_show_all = False
        self.signals = {}
        self.children = []
        self.properties = {}

    def connect(self, event, callback):
        self.signals[event] = callback

    def add(self, child):
        self.children.append(child)

    def pack_start(self, child, *_):
        self.add(child)

    def show_all(self):
        self.visible = True
        for child in self.children:
            if not child.no_show_all:
                child.show_all()

    def hide(self):
        self.visible = False

    def set_visible(self, value):
        self.visible = value

    def set_no_show_all(self, value):
        self.no_show_all = value

    def set_label(self, value):
        self.label = value

    def get_screen(self):
        return SimpleNamespace(get_rgba_visual=lambda: "rgba")

    def __getattr__(self, name):
        # Record toolkit configuration, not a replica of GTK layout behavior.
        if name.startswith("set_") or name in {"realize", "destroy", "add_events"}:
            return lambda *args: self.properties.update({name: args})
        raise AttributeError(name)


def test_native_panel_buttons_and_exclusive_edge_input_geometry():
    css = []
    gtk = SimpleNamespace(
        Window=Widget,
        Box=Widget,
        Button=Widget,
        Orientation=SimpleNamespace(HORIZONTAL=0),
        CssProvider=lambda: SimpleNamespace(load_from_data=css.append),
        StyleContext=SimpleNamespace(add_provider_for_screen=lambda *_: None),
        STYLE_PROVIDER_PRIORITY_APPLICATION=600,
    )
    gdk = SimpleNamespace(
        EventMask=SimpleNamespace(
            ENTER_NOTIFY_MASK=1, BUTTON_PRESS_MASK=2, TOUCH_MASK=4
        )
    )
    settings = {}

    def record(name):
        return lambda window, *args: (
            settings.setdefault(window, {}).setdefault(name, []).append(args)
        )

    layer = SimpleNamespace(
        Edge=SimpleNamespace(BOTTOM="bottom", LEFT="left", RIGHT="right"),
        Layer=SimpleNamespace(OVERLAY="overlay"),
        KeyboardMode=SimpleNamespace(NONE="none"),
        **{
            name: record(name)
            for name in (
                "init_for_window",
                "set_layer",
                "set_anchor",
                "set_exclusive_zone",
                "set_keyboard_mode",
            )
        },
    )
    events = []
    panel = Panel(gtk, gdk, layer, events.append)
    assert not panel.panel.visible and not panel.hotspot.visible
    assert panel.hotspot.properties["set_size_request"] == (1, 8)
    assert settings[panel.hotspot]["set_anchor"] == [
        ("bottom", True),
        ("left", True),
        ("right", True),
    ]
    for window in (panel.panel, panel.hotspot):
        assert settings[window]["set_exclusive_zone"] == [(0,)]
        assert settings[window]["set_layer"] == [("overlay",)]
        assert window.properties["set_visual"] == ("rgba",)
    assert b"background-color: transparent" in css[0]
    panel.update(ControlState(enabled=True))
    assert panel.hotspot.visible and not panel.panel.visible
    for name in ("enter-notify-event", "button-press-event", "touch-event"):
        assert panel.hotspot.signals[name]() is True
    assert events == ["activity"] * 3
    panel.update(ControlState(enabled=True, visible=True, paused=True))
    assert not panel.hotspot.visible and panel.panel.visible
    visible = {action for action, button in panel.controls.items() if button.visible}
    assert visible == {Action.PREVIOUS, Action.TOGGLE_PAUSE, Action.NEXT}
    assert panel.controls[Action.TOGGLE_PAUSE].label == "Play"
    panel.update(ControlState(enabled=True, visible=True, seekable=True, audio=True))
    events.clear()
    for action, button in panel.controls.items():
        assert button.visible
        button.signals["clicked"](button)
    assert events == [action.value for action in panel.controls]
    panel.update(ControlState())
    assert not panel.panel.visible and not panel.hotspot.visible
    events.clear()
    panel.reveal()
    assert not events
