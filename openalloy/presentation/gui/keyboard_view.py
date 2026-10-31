# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Interactive drawing of the Alloy Origins Core.

* renders the current lighting live (same pure effect code as the daemon);
* click toggles a key's selection, drag rubber-bands a selection;
* in brush mode click/drag paints keys with the brush colour;
* clicking or typing triggers reactive effects in the preview.
"""

from __future__ import annotations

import math
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango, PangoCairo  # noqa: E402

from openalloy.application.effects import Lighting, Renderer  # noqa: E402
from openalloy.domain.layout import BOARD_HEIGHT, BOARD_WIDTH, Layout, get_layout  # noqa: E402

RGB = Tuple[int, int, int]
PAD = 14
GAP = 0.08          # gap between caps, in units


def _rounded(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class KeyboardView(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.layout: Layout = get_layout("ansi")
        self.renderer: Optional[Renderer] = None
        self.colors: List[RGB] = [(0, 0, 0)] * 94
        self.selected: Set[int] = set()
        self.overlay: Dict[int, RGB] = {}
        self.brush: Optional[RGB] = None          # None = selection mode
        self.dimmed_text: Optional[str] = None
        self.on_selection_changed: Optional[Callable[[], None]] = None
        self.on_paint: Optional[Callable[[int, Optional[RGB]], None]] = None
        self.on_key_activate: Optional[Callable[[int], None]] = None
        self._t0 = time.monotonic()
        self._hover: Optional[int] = None
        self._drag_start: Optional[Tuple[float, float]] = None
        self._drag_rect: Optional[Tuple[float, float, float, float]] = None
        self._drag_base: Set[int] = set()
        self._painted_in_drag: Set[int] = set()

        self.set_hexpand(True)
        self.set_content_height(300)
        self.set_draw_func(self._draw)
        self.set_focusable(True)

        click = Gtk.GestureClick()
        click.connect("pressed", self._on_pressed)
        self.add_controller(click)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", lambda *_args: self._set_hover(None))
        self.add_controller(motion)

        self._tick = self.add_tick_callback(self._on_tick)

    # --- model -----------------------------------------------------------
    def set_layout(self, layout: Layout) -> None:
        self.layout = layout
        self.selected &= set(layout.indices)
        if self.renderer:
            self.set_lighting(self.renderer.lighting)
        self.queue_draw()

    def set_lighting(self, lighting: Lighting) -> None:
        try:
            self.renderer = Renderer(lighting, self.layout)
        except ValueError:
            return
        self.overlay = dict(lighting.keys)
        self.queue_draw()

    def trigger_key(self, index: int) -> None:
        if self.renderer:
            self.renderer.on_key(index, time.monotonic() - self._t0)

    def select(self, indices, add: bool = False) -> None:
        if not add:
            self.selected.clear()
        self.selected |= {i for i in indices if self.layout.key(i)}
        self._selection_changed()

    def clear_selection(self) -> None:
        self.selected.clear()
        self._selection_changed()

    def _selection_changed(self):
        self.queue_draw()
        if self.on_selection_changed:
            self.on_selection_changed()

    # --- geometry --------------------------------------------------------
    def _metrics(self):
        w, h = self.get_width(), self.get_height()
        unit = min((w - 2 * PAD) / BOARD_WIDTH, (h - 2 * PAD) / BOARD_HEIGHT)
        ox = (w - unit * BOARD_WIDTH) / 2
        oy = (h - unit * BOARD_HEIGHT) / 2
        return unit, ox, oy

    def _rects(self, key, unit, ox, oy):
        g = GAP / 2
        rects = [(ox + (key.x + g) * unit, oy + (key.y + g) * unit,
                  (key.w - GAP) * unit, (key.h - GAP) * unit)]
        if key.extra:
            ex, ey, ew, eh = key.extra
            rects.append((ox + (ex + g) * unit, oy + (ey - g) * unit,
                          (ew - GAP) * unit, (eh) * unit))
        return rects

    def key_at(self, x: float, y: float) -> Optional[int]:
        unit, ox, oy = self._metrics()
        for key in self.layout.keys:
            for rx, ry, rw, rh in self._rects(key, unit, ox, oy):
                if rx <= x <= rx + rw and ry <= y <= ry + rh:
                    return key.index
        return None

    # --- input -----------------------------------------------------------
    def _set_hover(self, idx):
        if idx != self._hover:
            self._hover = idx
            self.queue_draw()

    def _on_motion(self, _ctl, x, y):
        self._set_hover(self.key_at(x, y))

    def _on_pressed(self, gesture, _n_press, x, y):
        self.grab_focus()
        idx = self.key_at(x, y)
        if idx is None:
            return
        self.trigger_key(idx)
        if self.on_key_activate:
            self.on_key_activate(idx)
        if self.brush is not None:
            self._paint(idx)
            return
        state = gesture.get_current_event_state()
        additive = bool(state & (1 << 0)) or bool(state & (1 << 2))  # shift / ctrl
        if idx in self.selected and not additive:
            self.selected.discard(idx)
        elif additive:
            self.selected ^= {idx}
        else:
            self.selected.add(idx)
        self._selection_changed()

    def _paint(self, idx):
        if idx in self._painted_in_drag:
            return
        self._painted_in_drag.add(idx)
        if self.on_paint:
            self.on_paint(idx, self.brush)

    def _on_drag_begin(self, _g, x, y):
        self._drag_start = (x, y)
        self._drag_base = set(self.selected)
        self._painted_in_drag = set()

    def _on_drag_update(self, _g, dx, dy):
        if self._drag_start is None or (abs(dx) < 4 and abs(dy) < 4):
            return
        x0, y0 = self._drag_start
        if self.brush is not None:
            idx = self.key_at(x0 + dx, y0 + dy)
            if idx is not None:
                self._paint(idx)
            return
        self._drag_rect = (min(x0, x0 + dx), min(y0, y0 + dy), abs(dx), abs(dy))
        rx, ry, rw, rh = self._drag_rect
        unit, ox, oy = self._metrics()
        hit = set()
        for key in self.layout.keys:
            for kx, ky, kw, kh in self._rects(key, unit, ox, oy):
                if kx < rx + rw and kx + kw > rx and ky < ry + rh and ky + kh > ry:
                    hit.add(key.index)
        self.selected = self._drag_base | hit
        self._selection_changed()

    def _on_drag_end(self, *_args):
        self._drag_start = None
        self._drag_rect = None
        self._painted_in_drag = set()
        self.queue_draw()

    # --- animation & drawing ----------------------------------------------
    def _on_tick(self, _widget, _clock):
        if self.renderer is not None and self.get_mapped():
            self.colors = self.renderer.frame(time.monotonic() - self._t0)
            self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _draw(self, _area, cr, width, height):
        unit, ox, oy = self._metrics()
        # board
        cr.set_source_rgb(0.055, 0.058, 0.07)
        _rounded(cr, ox - unit * 0.35, oy - unit * 0.35, unit * (BOARD_WIDTH + 0.7),
                 unit * (BOARD_HEIGHT + 0.7), unit * 0.35)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.06)
        cr.set_line_width(1)
        _rounded(cr, ox - unit * 0.35, oy - unit * 0.35, unit * (BOARD_WIDTH + 0.7),
                 unit * (BOARD_HEIGHT + 0.7), unit * 0.35)
        cr.stroke()

        dim = 0.35 if self.dimmed_text else 1.0
        font = Pango.FontDescription.from_string(f"Sans Bold {max(6.0, unit * 0.21):.1f}")
        for key in self.layout.keys:
            r, g, b = (c / 255.0 * dim for c in self.colors[key.index])
            rects = self._rects(key, unit, ox, oy)
            for rx, ry, rw, rh in rects:
                # glow underneath
                cr.set_source_rgba(r, g, b, 0.35)
                _rounded(cr, rx - 1.5, ry - 1.5, rw + 3, rh + 3, unit * 0.16)
                cr.fill()
                # keycap: dark body tinted by the LED
                cr.set_source_rgb(0.09 + r * 0.72, 0.095 + g * 0.72, 0.11 + b * 0.72)
                _rounded(cr, rx, ry, rw, rh, unit * 0.12)
                cr.fill()
                # top face
                inset = unit * 0.09
                cr.set_source_rgba(1, 1, 1, 0.05)
                _rounded(cr, rx + inset, ry + inset * 0.6, rw - 2 * inset, rh - 2.2 * inset,
                         unit * 0.09)
                cr.fill()
            rx, ry, rw, rh = rects[0]
            if key.label and unit >= 24:
                lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
                cr.set_source_rgba(0.05, 0.05, 0.06, 0.9) if lum > 0.55 else \
                    cr.set_source_rgba(1, 1, 1, 0.85)
                lay = PangoCairo.create_layout(cr)
                lay.set_font_description(font)
                lay.set_text(key.label, -1)
                tw, th = lay.get_pixel_size()
                cr.move_to(rx + unit * 0.14, ry + unit * 0.1)
                if tw > rw - unit * 0.2:
                    cr.move_to(rx + (rw - tw) / 2, ry + (rh - th) / 2)
                PangoCairo.show_layout(cr, lay)
            if key.index in self.overlay:
                cr.set_source_rgba(1, 1, 1, 0.9)
                cr.arc(rx + rw - unit * 0.16, ry + rh - unit * 0.16, unit * 0.05, 0, 2 * math.pi)
                cr.fill()
            if key.index in self.selected or key.index == self._hover:
                sel = key.index in self.selected
                cr.set_source_rgba(1, 1, 1, 0.95 if sel else 0.4)
                cr.set_line_width(2.2 if sel else 1.2)
                if sel:
                    cr.set_dash([5, 3])
                for rx2, ry2, rw2, rh2 in rects:
                    _rounded(cr, rx2 + 1, ry2 + 1, rw2 - 2, rh2 - 2, unit * 0.12)
                    cr.stroke()
                cr.set_dash([])

        if self._drag_rect:
            x, y, w, h = self._drag_rect
            cr.set_source_rgba(0.4, 0.7, 1.0, 0.15)
            cr.rectangle(x, y, w, h)
            cr.fill_preserve()
            cr.set_source_rgba(0.4, 0.7, 1.0, 0.8)
            cr.set_line_width(1)
            cr.stroke()

        if self.dimmed_text:
            lay = PangoCairo.create_layout(cr)
            lay.set_font_description(Pango.FontDescription.from_string(
                f"Sans Bold {max(9.0, unit * 0.36):.1f}"))
            lay.set_text(self.dimmed_text, -1)
            tw, th = lay.get_pixel_size()
            cr.set_source_rgba(0, 0, 0, 0.65)
            _rounded(cr, (width - tw) / 2 - 16, (height - th) / 2 - 10, tw + 32, th + 20, 10)
            cr.fill()
            cr.set_source_rgb(1, 1, 1)
            cr.move_to((width - tw) / 2, (height - th) / 2)
            PangoCairo.show_layout(cr, lay)
