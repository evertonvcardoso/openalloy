# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Reusable GTK widgets: the auto-generated parameter form and colour helpers.

Linuity built one row per entry of a static PARAM_SPECS table and toggled
visibility per mode; here the rows are generated from each effect's
declared parameters, so new effects get a UI for free.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from openalloy.domain.colors import parse_color, to_hex  # noqa: E402
from openalloy.i18n import _  # noqa: E402


def rgba_from_hex(value: str) -> Gdk.RGBA:
    r, g, b = parse_color(value)
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r / 255, g / 255, b / 255, 1.0
    return c


def hex_from_rgba(c: Gdk.RGBA) -> str:
    return to_hex((c.red * 255, c.green * 255, c.blue * 255))


def color_button(value: str, on_change: Optional[Callable[[str], None]] = None
                 ) -> Gtk.ColorDialogButton:
    dlg = Gtk.ColorDialog()
    dlg.set_with_alpha(False)
    dlg.set_title(_("Choose a color"))
    btn = Gtk.ColorDialogButton.new(dlg)
    btn.set_rgba(rgba_from_hex(value))
    if on_change:
        btn.connect("notify::rgba", lambda b, _p: on_change(hex_from_rgba(b.get_rgba())))
    return btn


def labeled(text: str, widget: Gtk.Widget, help_text: str = "") -> Gtk.Box:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    label = Gtk.Label(label=text, xalign=0)
    label.set_size_request(150, -1)
    label.add_css_class("param-label")
    if help_text:
        label.set_tooltip_text(help_text)
        widget.set_tooltip_text(help_text)
    if isinstance(widget, Gtk.Scale):
        widget.set_hexpand(True)
    else:
        widget.set_hexpand(False)
        widget.set_halign(Gtk.Align.START)
        if isinstance(widget, (Gtk.DropDown, Gtk.SpinButton)):
            widget.set_size_request(240, -1)
    row.append(label)
    row.append(widget)
    return row


class ColorList(Gtk.Box):
    """A row of colour buttons with + / - (for multi-colour effects)."""

    def __init__(self, values: List[str], on_change: Callable[[List[str]], None]):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._on_change = on_change
        self._buttons: List[Gtk.ColorDialogButton] = []
        self._box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.append(self._box)
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text=_("Add color"))
        add.add_css_class("flat")
        add.connect("clicked", lambda *_args: self._add("#ffffff", emit=True))
        rem = Gtk.Button(icon_name="list-remove-symbolic", tooltip_text=_("Remove the last color"))
        rem.add_css_class("flat")
        rem.connect("clicked", self._remove)
        self.append(add)
        self.append(rem)
        for v in values:
            self._add(v, emit=False)

    def _add(self, value, emit):
        if len(self._buttons) >= 6:
            return
        btn = color_button(value, lambda _v: self._emit())
        self._buttons.append(btn)
        self._box.append(btn)
        if emit:
            self._emit()

    def _remove(self, *_args):
        if len(self._buttons) <= 1:
            return
        btn = self._buttons.pop()
        self._box.remove(btn)
        self._emit()

    def values(self) -> List[str]:
        return [hex_from_rgba(b.get_rgba()) for b in self._buttons]

    def _emit(self):
        self._on_change(self.values())


class ParamForm(Gtk.Box):
    """Builds one control per parameter spec and reports the values dict."""

    def __init__(self, on_change: Callable[[Dict[str, Any]], None]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._on_change = on_change
        self._getters: Dict[str, Callable[[], Any]] = {}
        self._building = False

    def build(self, specs: List[dict], values: Dict[str, Any]) -> None:
        self._building = True
        child = self.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.remove(child)
            child = nxt
        self._getters.clear()
        for spec in specs:
            name = spec["name"]
            val = values.get(name, spec["default"])
            try:
                widget, getter = self._make(spec, val)
            except (TypeError, ValueError, KeyError):
                # a hand-edited/imported preset with a bad value: show the default
                widget, getter = self._make(spec, spec["default"])
            self._getters[name] = getter
            # labels, help and choices are English msgids from the effect definitions
            self.append(labeled(_(spec["label"]), widget, _(spec["help"]) if spec.get("help") else ""))
        if not specs:
            hint = Gtk.Label(label=_("This effect has no parameters."), xalign=0)
            hint.add_css_class("dim-label")
            self.append(hint)
        self._building = False

    def values(self) -> Dict[str, Any]:
        return {k: g() for k, g in self._getters.items()}

    def _changed(self, *_args):
        if not self._building:
            self._on_change(self.values())

    def _make(self, spec: dict, val: Any):
        kind = spec["kind"]
        if kind == "color":
            btn = color_button(to_hex(parse_color(val)), lambda _v: self._changed())
            btn.set_hexpand(False)
            btn.set_halign(Gtk.Align.START)
            return btn, lambda: hex_from_rgba(btn.get_rgba())
        if kind == "colors":
            vals = [to_hex(parse_color(v)) for v in (val or spec["default"])]
            cl = ColorList(vals, lambda _v: self._changed())
            return cl, cl.values
        if kind == "int":
            sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, spec["min"], spec["max"],
                                          spec.get("step") or 1)
            sc.set_value(float(val))
            sc.set_draw_value(True)
            sc.set_digits(0)
            sc.set_value_pos(Gtk.PositionType.RIGHT)
            sc.connect("value-changed", self._changed)
            return sc, lambda: int(round(sc.get_value()))
        if kind == "float":
            sp = Gtk.SpinButton.new_with_range(spec["min"], spec["max"], spec.get("step") or 0.1)
            sp.set_digits(2)
            sp.set_value(float(val))
            sp.set_hexpand(False)
            sp.set_halign(Gtk.Align.START)
            sp.connect("value-changed", self._changed)
            return sp, lambda: round(sp.get_value(), 3)
        if kind == "choice":
            keys = list(spec["choices"].keys())
            dd = Gtk.DropDown.new_from_strings([_(spec["choices"][k]) for k in keys])
            dd.set_selected(keys.index(val) if val in keys else 0)
            dd.set_hexpand(False)
            dd.set_halign(Gtk.Align.START)
            dd.connect("notify::selected", self._changed)
            return dd, lambda: keys[dd.get_selected()]
        if kind == "bool":
            sw = Gtk.Switch(active=bool(val))
            sw.set_hexpand(False)
            sw.set_halign(Gtk.Align.START)
            sw.connect("notify::active", self._changed)
            return sw, sw.get_active
        lbl = Gtk.Label(label=str(val))
        return lbl, lambda: val
