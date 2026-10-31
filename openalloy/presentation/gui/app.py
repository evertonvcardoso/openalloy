# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""OpenAlloy GTK4 interface - simple, Linuity-style.

Follows the desktop's own GTK theme (no forced colours), three tabs and plain
buttons; less common options live in collapsed expanders. Every feature of
the engine is reachable:

  Lighting : effect + parameters, Apply / Fix on keyboard / LEDs off,
             per-key colours, key reaction, presets
  Keyboard : brightness, RGB test, give LEDs back, onboard memory (only when
             the firmware supports it), device info, settings, language, service
  Logs     : journalctl of the service, or this window's own log

Every text is English in the source and goes through openalloy.i18n; changing the
language rebuilds the window in place (the application keeps running).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from typing import Any, Callable, Dict, List, Optional

from openalloy.i18n import N_, _, ngettext, pgettext

log = logging.getLogger(__name__)


def pgettext_noop(context, message):
    """Marks a message with context for extraction; translated later with pgettext."""
    return message


def launch(simulate=False) -> int:
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk  # noqa: F401
    except (ImportError, ValueError):
        print(_("Error: GTK4/PyGObject not found. Install python3-gi, python3-gi-cairo and "
                "gir1.2-gtk-4.0 (Debian/Ubuntu/Sparky)."))
        return 1
    from openalloy.application.backend import connect
    backend = BackendHolder(connect(simulate=simulate), simulate)
    app = _build_app(backend, simulate)
    try:
        return app.run(None)
    finally:
        backend.close()


class BackendHolder:
    """The window talks to whichever backend currently owns the keyboard. When the
    user starts or stops the service from the window, the target is swapped
    (window <-> service) instead of leaving the window talking to nothing."""

    def __init__(self, backend, simulate=False):
        self.be = backend
        self.simulate = simulate
        self._listener = None
        self._registered = False
        self._lock = threading.Lock()
        self.generation = 0          # bumped on every swap: older replies are stale

    @property
    def kind(self):
        return self.be.kind

    def call(self, method, progress=None, **params):
        return self.be.call(method, progress=progress, **params)

    def subscribe(self, fn):
        """Only the newest window listens (a language switch rebuilds the window)."""
        self._listener = fn
        if not self._registered:
            self._registered = True
            self.be.subscribe(self._dispatch)

    def _dispatch(self, kind, data):
        fn = self._listener
        if fn is not None:
            fn(kind, data)

    def swap(self, new):
        with self._lock:
            old, self.be = self.be, new
            self.generation += 1
        try:
            old.close()
        except Exception:
            log.debug("close old backend", exc_info=True)
        if self._registered:
            new.subscribe(self._dispatch)

    def close(self):
        self.be.close()


# Only layout tweaks - colours always come from the user's theme.
CSS = b"""
.oa-section { font-weight: bold; }
.oa-mode { font-weight: bold; }
"""

DRIVER_NAMES = {"hid_lamparray": N_("HID LampArray (Dynamic Lighting)"),
                "hyperx_alloy_origins_core": N_("vendor channel")}
SHORT_DRIVER = {"hid_lamparray": N_("LampArray"), "hyperx_alloy_origins_core": N_("vendor channel")}
GROUP_LABELS = [("wasd", N_("WASD")), ("arrows", N_("Arrows")), ("letters", N_("Letters")),
                ("numbers", N_("Numbers")), ("frow", N_("F1–F12")),
                ("modifiers", N_("Modifiers")), ("nav", N_("Navigation")),
                ("__all", N_("All")), ("__none", pgettext_noop("keys", "None"))]


def _build_app(backend, simulate):
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, Gio, GLib, Gtk

    from openalloy import APP_ID, __version__, i18n
    from openalloy.application.effects import Lighting
    from openalloy.domain.colors import parse_color
    from openalloy.domain.layout import GROUPS, get_layout
    from openalloy.domain.sound import Sound
    from openalloy.presentation.gui.keyboard_view import KeyboardView
    from openalloy.presentation.gui.widgets import ParamForm, color_button, hex_from_rgba, labeled

    # --- helpers -----------------------------------------------------------------
    def run_async(fn: Callable[[], Any], done: Optional[Callable[[Any, Optional[str]], None]] = None,
                  owner=None, keep_on_swap: bool = False):
        """Run ``fn`` off the GTK thread and hand the result to ``done`` on it.

        With an ``owner`` window, the reply is dropped when that window was closed
        meanwhile (e.g. rebuilt for a language switch) or, unless ``keep_on_swap``,
        when the backend was swapped (service <-> window): such a reply describes a
        state that no longer exists."""
        gen = backend.generation

        def deliver(res, err):
            if owner is not None and (owner._closing or
                                      (not keep_on_swap and backend.generation != gen)):
                return False
            done(res, err)
            return False

        def worker():
            try:
                res, err = fn(), None
            except Exception as exc:          # shown to the user
                res, err = None, str(exc)
            if done:
                # DEFAULT, not idle priority: the keyboard preview redraws on every frame,
                # and on a slow or busy machine idle callbacks would never run (the lab
                # saw replies - e.g. a language switch - never arrive)
                GLib.idle_add(deliver, res, err, priority=GLib.PRIORITY_DEFAULT)
        threading.Thread(target=worker, daemon=True).start()

    def box(vertical=True, spacing=8, margin=0):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL,
                    spacing=spacing)
        if margin:
            b.set_margin_top(margin)
            b.set_margin_bottom(margin)
            b.set_margin_start(margin)
            b.set_margin_end(margin)
        return b

    def dim(text=""):
        lbl = Gtk.Label(label=text, xalign=0, wrap=True)
        lbl.add_css_class("dim-label")
        return lbl

    def frame(title, child):
        f = Gtk.Frame()
        lbl = Gtk.Label(label=title)
        lbl.add_css_class("oa-section")
        f.set_label_widget(lbl)
        child.set_margin_top(8)
        child.set_margin_bottom(10)
        child.set_margin_start(10)
        child.set_margin_end(10)
        f.set_child(child)
        return f

    def expander(title, child, expanded=False):
        e = Gtk.Expander(label=title, expanded=expanded)
        child.set_margin_top(6)
        child.set_margin_start(14)
        e.set_child(child)
        return e

    def scroller(child):
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_child(child)
        return sw

    class LogHandler(logging.Handler):
        def __init__(self, sink):
            super().__init__(logging.INFO)
            self.sink = sink
            self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

        def emit(self, record):
            msg = self.format(record)
            GLib.idle_add(lambda: (self.sink(msg), False)[1], priority=GLib.PRIORITY_DEFAULT)

    # --- window ----------------------------------------------------------------------
    class Window(Gtk.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title="OpenAlloy")
            self.set_default_size(980, 780)
            self.set_icon_name("openalloy")
            self.effects: List[dict] = []
            self.effect_ids: List[str] = []
            self.reaction_specs: List[dict] = []
            self.lighting: Dict[str, Any] = {"effect": "static", "params": {}, "keys": {},
                                             "reaction": {}}
            self.applied: Optional[dict] = None
            self.status: Dict[str, Any] = {}
            self.presets: List[dict] = []
            self.settings: Dict[str, Any] = {}
            self._quiet = False
            self._testing = False
            self._poll_busy = False     # one status request at a time
            self._closing = False
            self._snd = False
            self._bright_timer = 0
            self._log_proc = None
            self._log_stop = threading.Event()
            self.connect("close-request", self._on_close)

            self.set_titlebar(self._header())
            root = box(spacing=8, margin=12)
            self.set_child(root)

            self.view = KeyboardView()
            self.view.set_content_height(230)
            self.view.on_selection_changed = self._on_selection
            self.view.on_paint = self._on_paint
            self.view.on_key_activate = self._on_view_key
            root.append(self.view)

            self.mode_label = Gtk.Label(xalign=0, wrap=True)
            self.mode_label.add_css_class("oa-mode")
            root.append(self.mode_label)

            self.stack = Gtk.Stack()
            root.append(Gtk.StackSwitcher(stack=self.stack, halign=Gtk.Align.CENTER))
            root.append(self.stack)
            self.stack.add_titled(self._lighting_page(), "lighting", _("Lighting"))
            self.stack.add_titled(self._keyboard_page(), "keyboard", _("Keyboard"))
            self.stack.add_titled(self._logs_page(), "logs", _("Logs"))

            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", self._on_window_key)
            self.add_controller(keys)

            backend.subscribe(lambda k, d: GLib.idle_add(lambda: (self._on_event(k, d), False)[1],
                                                         priority=GLib.PRIORITY_DEFAULT))
            self._load()
            GLib.timeout_add_seconds(2, self._poll)
            GLib.timeout_add(66, self._sound_tick)
            start = os.environ.get("OPENALLOY_START_PAGE")
            if start:
                self.stack.set_visible_child_name(start)

        def _async(self, fn, done=None, keep_on_swap=False):
            run_async(fn, done, owner=self, keep_on_swap=keep_on_swap)

        # --- header --------------------------------------------------------------
        def _header(self):
            hb = Gtk.HeaderBar()
            titles = box(spacing=0)
            t = Gtk.Label(label="OpenAlloy")
            t.add_css_class("title")
            self.subtitle = dim(f"v{__version__}")
            self.subtitle.set_xalign(0.5)
            titles.append(t)
            titles.append(self.subtitle)
            hb.set_title_widget(titles)
            self.conn_label = dim(_("connecting…"))
            self.conn_label.set_wrap(False)
            hb.pack_end(self.conn_label)
            return hb

        # --- Lighting -----------------------------------------------------------
        def _lighting_page(self):
            page = box(spacing=12)
            page.set_margin_top(6)

            self.effect_dd = Gtk.DropDown.new_from_strings(["…"])
            self.effect_dd.connect("notify::selected", self._on_effect)
            page.append(labeled(_("Effect"), self.effect_dd))
            self.effect_desc = dim()
            page.append(self.effect_desc)
            self.form = ParamForm(self._on_params)
            page.append(self.form)

            keys = box(spacing=8)
            keys.append(dim(_("Click or drag on the keyboard to select (Ctrl adds). Per-key "
                              "colors sit on top of any effect.")))
            row = box(False, 6)
            self.brush_btn = Gtk.ToggleButton(label=_("Brush"))
            self.brush_btn.set_tooltip_text(_("On: clicking/dragging paints directly with the "
                                              "color next to it"))
            self.brush_btn.connect("toggled", self._on_brush)
            self.brush_color = color_button("#ffffff", lambda _v: self._on_brush(self.brush_btn))
            paint = Gtk.Button(label=_("Paint selection"))
            paint.connect("clicked", self._paint_selection)
            unpaint = Gtk.Button(label=_("Remove color"))
            unpaint.connect("clicked", self._unpaint_selection)
            clear = Gtk.Button(label=_("Clear all"))
            clear.connect("clicked", self._clear_keys)
            for w in (self.brush_btn, self.brush_color, paint, unpaint, clear):
                row.append(w)
            keys.append(row)
            row = box(False, 6)
            self.group_dd = Gtk.DropDown.new_from_strings([
                pgettext("keys", lbl) if gid == "__none" else _(lbl) for gid, lbl in GROUP_LABELS])
            sel = Gtk.Button(label=_("Select"))
            sel.connect("clicked", self._select_group)
            self.sel_label = dim(_("No key selected"))
            row.append(Gtk.Label(label=_("Group:")))
            row.append(self.group_dd)
            row.append(sel)
            row.append(self.sel_label)
            keys.append(row)
            page.append(expander(_("Per-key colors"), keys))

            react = box(spacing=8)
            react.append(dim(_("The pressed key lights up or sends out a wave. Live mode only.")))
            self.react_form = ParamForm(self._on_reaction)
            react.append(self.react_form)
            page.append(expander(_("Key reaction"), react))

            pre = box(False, 6)
            self.preset_dd = Gtk.DropDown.new_from_strings(["…"])
            self.preset_dd.set_hexpand(True)
            load = Gtk.Button(label=_("Load"))
            load.connect("clicked", self._preset_load)
            save = Gtk.MenuButton(label=_("Save as…"))
            pop = Gtk.Popover()
            pbox = box(False, 6, margin=6)
            self.preset_entry = Gtk.Entry(placeholder_text=_("Preset name"))
            ok = Gtk.Button(label=_("Save"))
            ok.connect("clicked", lambda *_args: self._preset_save(pop))
            self.preset_entry.connect("activate", lambda *_args: self._preset_save(pop))
            pbox.append(self.preset_entry)
            pbox.append(ok)
            pop.set_child(pbox)
            save.set_popover(pop)
            delete = Gtk.Button(label=_("Delete"))
            delete.connect("clicked", self._preset_delete)
            for w in (self.preset_dd, load, save, delete):
                pre.append(w)
            page.append(expander(_("Presets"), pre))

            # Action buttons stay visible below the scrolling options.
            outer = box(spacing=6)
            outer.append(scroller(page))
            outer.append(Gtk.Separator())
            actions = box(False, 8)
            self.apply_btn = Gtk.Button(label=_("Apply"))
            self.apply_btn.add_css_class("suggested-action")
            self.apply_btn.set_tooltip_text(_("Live mode: OpenAlloy keeps the effect running "
                                              "(needed for animated effects)."))
            self.apply_btn.connect("clicked", lambda *_args: self._apply())
            self.fix_btn = Gtk.Button(label=_("Fix on keyboard"))
            self.fix_btn.set_tooltip_text(_("Applies once and leaves the color on the keyboard, "
                                            "with nothing streaming. Animated effects stay still."))
            self.fix_btn.connect("clicked", lambda *_args: self._fix())
            off = Gtk.Button(label=_("LEDs off"))
            off.connect("clicked", lambda *_args: self._fix({"effect": "off", "params": {}, "keys": {},
                                                          "reaction": {}}))
            for w in (self.apply_btn, self.fix_btn, off):
                actions.append(w)
            outer.append(actions)
            self.msg = dim()
            outer.append(self.msg)
            return outer

        # --- Keyboard ---------------------------------------------------------------
        def _keyboard_page(self):
            page = box(spacing=12)
            page.set_margin_top(6)

            hw = box(spacing=8)
            self.bright = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
            self.bright.set_draw_value(True)
            self.bright.set_value_pos(Gtk.PositionType.RIGHT)
            self.bright.connect("value-changed", self._on_brightness)
            hw.append(labeled(_("Brightness (%)"), self.bright))
            row = box(False, 6)
            test = Gtk.Button(label=_("Test RGB"))
            test.set_tooltip_text(_("Shows a known pattern to confirm the keyboard accepts "
                                    "the commands"))
            test.connect("clicked", self._live_test)
            give = Gtk.Button(label=_("Give lighting back to the keyboard"))
            give.set_tooltip_text(_("Stops OpenAlloy and returns to the keyboard's own lighting"))
            give.connect("clicked", self._release)
            row.append(test)
            row.append(give)
            hw.append(row)
            page.append(frame(_("Hardware"), hw))

            mem = box(spacing=8)
            self.gate_label = dim()
            mem.append(self.gate_label)
            row = box(False, 6)
            self.slot_dd = Gtk.DropDown.new_from_strings(
                [_("Profile {n}").format(n=n) for n in (1, 2, 3)])
            activate = Gtk.Button(label=_("Activate"))
            activate.connect("clicked", self._activate_slot)
            read = Gtk.Button(label=_("Read"))
            read.connect("clicked", self._read_slot)
            self.write_btn = Gtk.Button(label=_("Write current effect"))
            self.write_btn.connect("clicked", self._confirm_write)
            for w in (self.slot_dd, activate, read, self.write_btn):
                row.append(w)
            mem.append(row)
            row = box(False, 6)
            self.frames_spin = Gtk.SpinButton.new_with_range(1, 255, 1)
            self.frames_spin.set_value(60)
            self.kp_keys = ["match", "black", "keep", "custom"]
            self.kp_dd = Gtk.DropDown.new_from_strings([_("On key press: same as the effect"),
                                                        _("On key press: turn off"),
                                                        _("On key press: keep"),
                                                        _("On key press: color")])
            self.kp_color = color_button("#ffffff")
            row.append(Gtk.Label(label=_("Frames:")))
            row.append(self.frames_spin)
            row.append(self.kp_dd)
            row.append(self.kp_color)
            mem.append(row)
            self.progress = Gtk.ProgressBar(show_text=True, visible=False)
            mem.append(self.progress)
            self.mem_frame = frame(_("Keyboard memory"), mem)
            self.mem_frame.set_visible(False)
            page.append(self.mem_frame)

            info = Gtk.Grid(column_spacing=16, row_spacing=4)
            self.info = {}
            for r, (k, label) in enumerate([("name", _("Model")), ("usb", _("USB")),
                                            ("firmware", _("Firmware")), ("layout", _("Layout")),
                                            ("driver", _("Driver")), ("control", _("Control"))]):
                info.attach(dim(label), 0, r, 1, 1)
                v = Gtk.Label(label="—", xalign=0, selectable=True)
                info.attach(v, 1, r, 1, 1)
                self.info[k] = v
            page.append(frame(_("Information"), info))

            st = box(spacing=8)
            self.layout_keys = [None, "ansi", "abnt2", "iso"]
            self.layout_dd = Gtk.DropDown.new_from_strings(
                [_("Automatic"), _("ANSI (US)"), _("ABNT2 (Brazil)"), _("ISO (Europe)")])
            self.layout_dd.connect("notify::selected", lambda *_args: self._setting("layout_override"))
            st.append(labeled(_("Layout"), self.layout_dd))
            self.lang_codes = list(i18n.LANGUAGES)
            self.lang_dd = Gtk.DropDown.new_from_strings(list(i18n.LANGUAGES.values()))
            self.lang_dd.connect("notify::selected", self._on_language)
            st.append(labeled(_("Language"), self.lang_dd))
            self.fps_spin = Gtk.SpinButton.new_with_range(5, 60, 1)
            self.fps_spin.connect("value-changed", lambda *_args: self._setting("fps"))
            st.append(labeled(_("Frames/s (live)"), self.fps_spin))
            self.reactive_sw = Gtk.Switch()
            self.reactive_sw.connect("notify::active", lambda *_args: self._setting("reactive_input"))
            st.append(labeled(_("Read keys (reaction)"), self.reactive_sw))
            self.restore_sw = Gtk.Switch()
            self.restore_sw.connect("notify::active", lambda *_args: self._setting("restore_brightness"))
            st.append(labeled(_("Restore brightness"), self.restore_sw))
            self.gamma_sw = Gtk.Switch()
            self.gamma_sw.set_tooltip_text(
                _("Converts screen colors for the LEDs. Without it, shades like the color "
                  "picker's red look pink on the keyboard."))
            self.gamma_sw.connect("notify::active", lambda *_args: self._setting("led_gamma"))
            st.append(labeled(_("Correct colors for LEDs"), self.gamma_sw))
            self.fn_box = box(spacing=8)
            self.pk_keys = ["release", "bind", "ignore"]
            self.pk_dd = Gtk.DropDown.new_from_strings(
                [_("Show the keyboard profile"), _("Switch to the bound preset"), _("Ignore")])
            self.pk_dd.connect("notify::selected", lambda *_args: self._setting("profile_key_action"))
            self.fn_box.append(labeled(_("Fn+1/2/3 keys"), self.pk_dd))
            self.bind_dds = {}
            for p in ("1", "2", "3"):
                dd = Gtk.DropDown.new_from_strings([pgettext("preset", "None")])
                dd.connect("notify::selected", lambda *_args: self._setting("profile_bindings"))
                self.bind_dds[p] = dd
                self.fn_box.append(labeled(_("Preset for Fn+{n}").format(n=p), dd))
            self.fn_box.set_visible(False)
            st.append(self.fn_box)
            row = box(False, 6)
            for label, verb in ((_("Start the service with the system"), ["enable", "--now"]),
                                (_("Stop the service"), ["disable", "--now"])):
                b = Gtk.Button(label=label)
                b.connect("clicked", lambda _b, v=verb: self._service(v))
                row.append(b)
            st.append(row)
            self.service_label = dim(_("The service keeps animated effects running and re-applies "
                                       "the fixed color when the PC starts, the keyboard is "
                                       "replugged or the PC wakes up."))
            st.append(self.service_label)
            page.append(expander(_("Settings"), st))
            return scroller(page)

        # --- Logs (from Linuity) --------------------------------------------------
        def _logs_page(self):
            self.log_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
            self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            sw = scroller(self.log_view)
            if backend.kind == "daemon" and shutil.which("journalctl"):
                try:
                    self._log_proc = subprocess.Popen(
                        ["journalctl", "--user", "-u", "openalloy.service", "-f", "-n", "50",
                         "--no-pager", "--output=short"],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                    threading.Thread(target=self._pump_journal, daemon=True).start()
                except OSError:
                    pass
            else:
                self._log_handler = LogHandler(self._log)
                logging.getLogger().addHandler(self._log_handler)
                logging.getLogger().setLevel(logging.INFO)
            return sw

        def _pump_journal(self):
            for line in self._log_proc.stdout:
                if self._log_stop.is_set():
                    break
                GLib.idle_add(lambda ln=line.rstrip(): (self._log(ln), False)[1],
                              priority=GLib.PRIORITY_DEFAULT)

        def _log(self, line):
            buf = self.log_view.get_buffer()
            buf.insert(buf.get_end_iter(), line + "\n")
            if buf.get_line_count() > 2000:
                res = buf.get_iter_at_line(500)
                buf.delete(buf.get_start_iter(), res[1] if isinstance(res, tuple) else res)

        # --- data loading -----------------------------------------------------------
        def _load(self):
            self._async(lambda: {"effects": backend.call("effects"),
                               "reaction": backend.call("reaction_params"),
                               "status": backend.call("status"),
                               "presets": backend.call("presets_list"),
                               "settings": backend.call("settings_get")}, self._loaded)

        def _loaded(self, data, err):
            if err:
                self.msg.set_text(_("Error while starting: {error}").format(error=err))
                return
            self.effects = data["effects"]
            self.effect_ids = [e["id"] for e in self.effects]
            self.reaction_specs = data["reaction"]
            labels = [_(e["label"]) + (" " + _("(sound)") if e.get("audio") else
                                       " " + _("(keys)") if e["live_only"] else "")
                      for e in self.effects]
            self._quiet = True
            self.effect_dd.set_model(Gtk.StringList.new(labels))
            self._quiet = False
            self.lighting = dict(data["status"]["lighting"])
            self.applied = dict(self.lighting)
            self._status(data["status"])
            self._show_lighting()
            self._set_presets(data["presets"])
            self._apply_settings(data["settings"])
            self._refresh_device()

        def _effect(self, eid=None):
            eid = eid or self.lighting.get("effect")
            return next((e for e in self.effects if e["id"] == eid), self.effects[0])

        def _show_lighting(self):
            if not self.effects:          # data not loaded yet (a "lighting" event can come
                return                    # first): _loaded() shows it
            self._quiet = True
            eid = self.lighting.get("effect")
            if eid in self.effect_ids:
                self.effect_dd.set_selected(self.effect_ids.index(eid))
            eff = self._effect()
            self.effect_desc.set_text(_(eff["description"]))
            self.form.build(eff["params"], self.lighting.get("params") or {})
            self.react_form.build(self.reaction_specs, self.lighting.get("reaction") or {})
            self._quiet = False
            self.lighting["params"] = self.form.values()
            self.lighting["reaction"] = self.react_form.values()
            self._preview()

        def _preview(self):
            try:
                self.view.set_lighting(Lighting.from_dict(self.lighting))
            except Exception as exc:
                log.debug("preview: %s", exc)
            self._sync_buttons()

        def _sync_buttons(self):
            live_same = self.applied == self.lighting and self.status.get("mode") == "live"
            self.apply_btn.set_label(_("Already applied") if live_same else _("Apply"))
            self.apply_btn.set_sensitive(not live_same)

        # --- lighting events -----------------------------------------------------
        def _on_effect(self, dd, _p):
            if self._quiet or not self.effect_ids:
                return
            eid = self.effect_ids[dd.get_selected()]
            if eid == self.lighting.get("effect"):
                return
            names = {p["name"] for p in self._effect(eid)["params"]}
            old = self.lighting.get("params") or {}
            self.lighting = {"effect": eid, "params": {k: v for k, v in old.items() if k in names},
                             "keys": self.lighting.get("keys", {}),
                             "reaction": self.lighting.get("reaction", {})}
            self._show_lighting()

        def _on_params(self, values):
            if self._quiet:
                return
            self.lighting["params"] = values
            self._preview()

        def _on_reaction(self, values):
            if self._quiet:
                return
            self.lighting["reaction"] = values
            self._preview()

        def _light_copy(self):
            return {"effect": self.lighting["effect"], "params": dict(self.lighting["params"]),
                    "keys": dict(self.lighting.get("keys") or {}),
                    "reaction": dict(self.lighting.get("reaction") or {})}

        def _apply(self):
            light = self._light_copy()

            def done(res, err):
                if err:
                    self.msg.set_text(_("Error: {error}").format(error=err))
                    return
                self.applied = res
                self.status["mode"] = "live"
                self.msg.set_text(_("Applied live.") if backend.kind == "daemon" else
                                  _("Applied live. It stays active while this window is open "
                                    "(or turn on the service in Keyboard → Settings)."))
                self._mode_label()
                self._sync_buttons()
            self._async(lambda: backend.call("apply", lighting=light, persist=True), done)

        def _fix(self, light=None):
            off = light is not None
            light = light or self._light_copy()
            animated = self._effect(light["effect"]).get("animated")

            def done(res, err):
                if err:
                    self.msg.set_text(_("Error: {error}").format(error=err))
                    return
                self.applied = res
                self.status["mode"] = "fixed"
                if off:
                    self.lighting = dict(res)
                    self._show_lighting()
                text = (_("LEDs turned off. You can close the window.") if off else
                        _("Color fixed on the keyboard. You can close the window."))
                if animated and not off:
                    text += " " + _("Animated effect: it stays on its first frame.")
                self.msg.set_text(text)
                self._mode_label()
                self._sync_buttons()
            self._async(lambda: backend.call("fix", lighting=light, persist=True), done)

        def _release(self, *_args):
            def done(_r, err):
                if err:
                    self.msg.set_text(_("Error: {error}").format(error=err))
                    return
                self.status["mode"] = "onboard"
                self._mode_label()
                self._sync_buttons()
            self._async(lambda: backend.call("release"), done)

        # --- per-key colours -----------------------------------------------------
        def _brush_hex(self):
            return hex_from_rgba(self.brush_color.get_rgba())

        def _on_brush(self, btn):
            self.view.brush = parse_color(self._brush_hex()) if btn.get_active() else None

        def _on_paint(self, idx, _c):
            self.lighting.setdefault("keys", {})[str(idx)] = self._brush_hex()
            self._preview()

        def _paint_selection(self, *_args):
            if not self.view.selected:
                self.msg.set_text(_("Select keys on the keyboard drawing first."))
                return
            for i in self.view.selected:
                self.lighting.setdefault("keys", {})[str(i)] = self._brush_hex()
            self._preview()

        def _unpaint_selection(self, *_args):
            for i in self.view.selected:
                self.lighting.setdefault("keys", {}).pop(str(i), None)
            self._preview()

        def _clear_keys(self, *_args):
            self.lighting["keys"] = {}
            self._preview()

        def _select_group(self, *_args):
            gid = GROUP_LABELS[self.group_dd.get_selected()][0]
            if gid == "__all":
                self.view.select(self.view.layout.indices)
            elif gid == "__none":
                self.view.clear_selection()
            else:
                idx = [self.view.layout.resolve(n) for n in GROUPS[gid]]
                self.view.select([i for i in idx if i is not None])

        def _on_selection(self):
            n = len(self.view.selected)
            self.sel_label.set_text(_("No key selected") if not n else
                                    ngettext("{n} key selected", "{n} keys selected", n).format(n=n))

        def _wants_keys(self):
            eff = self._effect()
            react = (self.lighting.get("reaction") or {}).get("type", "none") != "none"
            return (eff.get("live_only") and not eff.get("audio")) or react

        def _on_view_key(self, idx):
            if self._wants_keys() and self.status.get("mode") == "live":
                self._async(lambda: backend.call("key_pressed", index=idx))

        def _on_window_key(self, _c, _kv, keycode, _st):
            k = self.view.layout.by_evdev(keycode - 8)
            if k is not None:
                self.view.trigger_key(k.index)
            return False

        def _sound_tick(self):
            if self._closing:
                return False
            if not self.effects or not self._effect().get("audio") or self._snd:
                return True
            self._snd = True

            def done(res, _err):
                self._snd = False
                if res and self.view.renderer is not None:
                    self.view.renderer.set_sound(Sound.from_dict(res))
            def fetch():
                try:
                    return backend.call("sound_frame")
                finally:
                    self._snd = False          # freed even if the reply is dropped
            self._async(fetch, done)
            return True

        # --- presets ----------------------------------------------------------------
        def _set_presets(self, presets):
            self.presets = presets
            self.preset_dd.set_model(Gtk.StringList.new(
                [pgettext("preset", p["name"]) if p["builtin"] else p["name"] + " " + _("(yours)")
                 for p in presets]
                or [_("(none)")]))
            ids = [None] + [p["id"] for p in presets]
            self._quiet = True
            for key, dd in self.bind_dds.items():
                cur = (self.settings.get("profile_bindings") or {}).get(key)
                dd.set_model(Gtk.StringList.new(
                    [pgettext("preset", "None")]
                    + [pgettext("preset", p["name"]) if p["builtin"] else p["name"] for p in presets]))
                dd.set_selected(ids.index(cur) if cur in ids else 0)
            self._quiet = False

        def _selected_preset(self):
            idx = self.preset_dd.get_selected()     # may be GTK_INVALID_LIST_POSITION
            if not self.presets or not 0 <= idx < len(self.presets):
                self.msg.set_text(_("Select a valid preset."))
                return None
            return self.presets[idx]

        def _preset_load(self, *_args):
            preset = self._selected_preset()
            if preset is None:
                return
            p = preset["lighting"]
            self.lighting = {"effect": p.get("effect", "static"),
                             "params": dict(p.get("params") or {}),
                             "keys": dict(p.get("keys") or {}),
                             "reaction": dict(p.get("reaction") or {})}
            self._show_lighting()
            self.msg.set_text(_("Preset loaded: click Apply or Fix on keyboard."))

        def _preset_save(self, pop):
            name = self.preset_entry.get_text().strip()
            if not name:
                return
            pop.popdown()
            light = self._light_copy()
            self._async(lambda: (backend.call("preset_save", name=name, lighting=light),
                               backend.call("presets_list"))[1],
                      lambda res, err: self.msg.set_text(err) if err else
                      (self._set_presets(res),
                       self.msg.set_text(_("Preset “{name}” saved.").format(name=name))))

        def _preset_delete(self, *_args):
            p = self._selected_preset()
            if p is None:
                return
            if p["builtin"]:
                self.msg.set_text(_("Built-in presets cannot be deleted."))
                return
            self._async(lambda: (backend.call("preset_delete", ident=p["id"]),
                               backend.call("presets_list"))[1],
                      lambda res, err: self.msg.set_text(err) if err else self._set_presets(res))

        # --- keyboard page actions ------------------------------------------------
        def _on_brightness(self, scale):
            if self._quiet:
                return
            if self._bright_timer:
                GLib.source_remove(self._bright_timer)

            def fire():
                self._bright_timer = 0
                level = round(scale.get_value() * 255 / 100)
                self._async(lambda: backend.call("set_brightness", level=level),
                          lambda _r, e: e and self.msg.set_text(e))
                return False
            self._bright_timer = GLib.timeout_add(200, fire)

        def _live_test(self, *_args):
            def shown(res, err):
                if err:
                    self._testing = False
                    self.msg.set_text(_("Test not started: {error}").format(error=err))
                    return
                self.view.set_lighting(Lighting.from_dict(res["lighting"]))
                dlg = Gtk.AlertDialog()
                dlg.set_message(_("Look at the keyboard"))
                dlg.set_detail(_("Expected: {pattern}\n\nDoes it show that?").format(
                    pattern=res["expected"]))
                dlg.set_buttons([_("No"), _("Yes")])
                dlg.set_cancel_button(0)
                dlg.set_default_button(1)

                def chosen(d, r):
                    try:
                        ok = d.choose_finish(r) == 1
                    except GLib.Error:
                        ok = False
                    self._async(lambda: backend.call("confirm_live_test", ok=ok),
                              lambda _st, e: self._test_done(ok, e))
                dlg.choose(self, None, chosen)
            self._testing = True
            self._async(lambda: backend.call("live_test"), shown)

        def _test_done(self, ok, err):
            self._testing = False
            self._preview()
            self.msg.set_text(_("Error: {error}").format(error=err) if err else
                              _("Live RGB confirmed.") if ok else
                              _("RGB did not work: run 'openalloy doctor' and send the output."))
            self._refresh_flash()

        def _activate_slot(self, *_args):
            p = self.slot_dd.get_selected() + 1
            self._async(lambda: (backend.call("set_profile", profile=p), backend.call("release")),
                      lambda _r, e: self.gate_label.set_text(
                          e or _("Profile {profile} active on the keyboard.").format(profile=p)))

        def _read_slot(self, *_args):
            p = self.slot_dd.get_selected() + 1

            def done(cols, err):
                if err:
                    self.gate_label.set_text(_("Read error: {error}").format(error=err))
                    return
                counts: Dict[str, int] = {}
                for k in self.view.layout.keys:
                    counts[cols[k.index]] = counts.get(cols[k.index], 0) + 1
                top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
                self.gate_label.set_text(_("Profile {profile}, color on key press: {colors}").format(
                    profile=p, colors=", ".join(f"{n}× {c}" for c, n in top)))
            self._async(lambda: backend.call("read_onboard", profile=p), done)

        def _confirm_write(self, *_args):
            p = self.slot_dd.get_selected() + 1
            dlg = Gtk.AlertDialog()
            dlg.set_message(_("Write “{effect}” to profile {profile}?").format(
                effect=_(self._effect()["label"]), profile=p))
            dlg.set_detail(_("The current content of that profile in the keyboard memory "
                             "will be replaced."))
            dlg.set_buttons([_("Cancel"), _("Write")])
            dlg.set_cancel_button(0)
            dlg.set_default_button(1)

            def chosen(d, r):
                try:
                    if d.choose_finish(r) == 1:
                        self._write(p)
                except GLib.Error:
                    pass
            dlg.choose(self, None, chosen)

        def _write(self, p):
            light = self._light_copy()
            frames = int(self.frames_spin.get_value())
            kp = self.kp_keys[self.kp_dd.get_selected()]
            kpc = hex_from_rgba(self.kp_color.get_rgba())
            self.progress.set_visible(True)

            def progress(f, m):
                GLib.idle_add(lambda: (self.progress.set_fraction(f), self.progress.set_text(m),
                                       False)[2], priority=GLib.PRIORITY_DEFAULT)

            def done(res, err):
                self.progress.set_visible(False)
                self.gate_label.set_text(
                    _("Failed: {error}").format(error=err) if err else
                    ngettext("Profile {profile} written ({n} frame).",
                             "Profile {profile} written ({n} frames).", res["frames"])
                    .format(profile=p, n=res["frames"]))
            self._async(lambda: backend.call("upload_onboard", progress=progress, profile=p,
                                           lighting=light, frames=frames, keypress=kp,
                                           keypress_color=kpc), done)

        def _refresh_flash(self):
            def done(st, err):
                if err or not st:
                    return
                supported = st.get("supported", False)
                self.mem_frame.set_visible(supported)
                self.write_btn.set_sensitive(st["allowed"])
                if supported:
                    self.gate_label.set_text(_("Writing unlocked.") if st["allowed"] else
                                             _("Writing blocked: {reason}").format(
                                                 reason=st["reason"]))
            self._async(lambda: backend.call("flash_status"), done)

        def _refresh_device(self):
            def done(ds, _err):
                dev = (ds or {}).get("info") or self.status.get("device") or {}
                self.info["name"].set_text(dev.get("name", "—"))
                self.info["usb"].set_text(dev.get("usb_id", "—"))
                self.info["firmware"].set_text(dev.get("firmware", "—"))
                self.info["layout"].set_text((self.status.get("layout") or "—").upper())
                drv = dev.get("driver")
                self.info["driver"].set_text(_(DRIVER_NAMES[drv]) if drv in DRIVER_NAMES
                                             else drv or "—")
                self.info["control"].set_text(
                    (_("background service") if backend.kind == "daemon" else _("this window"))
                    + (" " + _("(simulated)") if dev.get("simulated") else ""))
                caps = dev.get("capabilities") or {}
                self.fn_box.set_visible(bool(caps.get("key_notifications")))
                if ds and ds.get("brightness") is not None:
                    self._quiet = True
                    self.bright.set_value(round(ds["brightness"] * 100 / 255))
                    self._quiet = False
                self._refresh_flash()
            self._async(lambda: backend.call("device_state"), done)

        # --- settings ---------------------------------------------------------------
        def _apply_settings(self, s):
            self.settings = s
            self._quiet = True
            lo = s.get("layout_override")
            self.layout_dd.set_selected(self.layout_keys.index(lo) if lo in self.layout_keys else 0)
            self.fps_spin.set_value(s.get("fps", 30))
            self.reactive_sw.set_active(bool(s.get("reactive_input", True)))
            self.restore_sw.set_active(s.get("restore_brightness") is not None)
            self.gamma_sw.set_active(float(s.get("led_gamma", 2.2) or 1.0) > 1.0)
            pk = s.get("profile_key_action", "release")
            self.pk_dd.set_selected(self.pk_keys.index(pk) if pk in self.pk_keys else 0)
            self.frames_spin.set_value(s.get("onboard_frames", 60))
            lang = s.get("language") or i18n.DEFAULT
            self.lang_dd.set_selected(self.lang_codes.index(lang) if lang in self.lang_codes else 0)
            self._quiet = False

        def _setting(self, key):
            if self._quiet:
                return
            ids = [None] + [p["id"] for p in self.presets]
            val = {
                "layout_override": lambda: self.layout_keys[self.layout_dd.get_selected()],
                "fps": lambda: int(self.fps_spin.get_value()),
                "led_gamma": lambda: 2.2 if self.gamma_sw.get_active() else 1.0,
                "reactive_input": lambda: self.reactive_sw.get_active(),
                "restore_brightness": lambda: (round(self.bright.get_value() * 255 / 100)
                                               if self.restore_sw.get_active() else None),
                "profile_key_action": lambda: self.pk_keys[self.pk_dd.get_selected()],
                "profile_bindings": lambda: {p: ids[dd.get_selected()] if dd.get_selected() < len(ids)
                                             else None for p, dd in self.bind_dds.items()},
            }[key]()

            def done(res, err):
                if err:
                    self.msg.set_text(err)
                    return
                self.settings = res
                if key == "layout_override":
                    self._poll()
            self._async(lambda: backend.call("settings_update", **{key: val}), done)

        def _on_language(self, dd, _p):
            if self._quiet:
                return
            code = self.lang_codes[dd.get_selected()] if dd.get_selected() < len(self.lang_codes) \
                else i18n.DEFAULT

            def done(res, err):
                if err:
                    self.msg.set_text(_("Error: {error}").format(error=err))
                    return
                i18n.set_language(code)
                self._rebuild()
            self._async(lambda: backend.call("settings_update", language=code), done)

        def _rebuild(self):
            """Recreate the window in the new language; the application keeps running."""
            app = self.get_application()
            page = self.stack.get_visible_child_name()
            new = Window(app)
            new.stack.set_visible_child_name(page or "keyboard")
            new.present()
            self.close()

        def _service(self, verb):
            if not shutil.which("systemctl"):
                self.service_label.set_text(_("systemctl not found."))
                return
            if backend.simulate:
                self.service_label.set_text(_("Unavailable in simulated mode."))
                return
            enabling = verb[0] == "enable"
            self.service_label.set_text(_("Starting the service…") if enabling else
                                        _("Stopping the service…"))

            def go():
                import time

                from openalloy.application.backend import LocalBackend, RemoteBackend
                from openalloy.config.settings import socket_path
                from openalloy.infra.rpc import is_alive
                if enabling and backend.kind == "local":
                    backend.be.close()          # let the service own the keyboard
                r = subprocess.run(["systemctl", "--user", *verb, "openalloy.service"],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    if enabling and backend.kind == "local":
                        backend.swap(LocalBackend())
                    return False, (r.stderr or r.stdout).strip() or _("Failed.")
                if enabling:
                    for _attempt in range(50):
                        if is_alive(socket_path()):
                            backend.swap(RemoteBackend())
                            return True, _("Service running: it keeps the lighting even with "
                                           "the window closed and starts with the system.")
                        time.sleep(0.2)
                    backend.swap(LocalBackend())
                    return False, _("The service did not answer; this window keeps controlling "
                                    "the keyboard. See the Logs tab or 'openalloy doctor'.")
                if backend.kind == "daemon":
                    backend.swap(LocalBackend())
                return True, _("Service stopped. This window controls the keyboard while it "
                               "is open.")

            def done(res, err):
                self.service_label.set_text(err or res[1])
                self._load()
            self._async(go, done, keep_on_swap=True)

        # --- status -----------------------------------------------------------------
        def _poll(self):
            if self._closing:
                return False
            if self._poll_busy:              # a slow service must not pile requests up
                return True
            self._poll_busy = True

            def done(st, err):
                self._poll_busy = False
                if self._closing:
                    return
                if st:
                    self._status(st)
                elif err:
                    self.conn_label.set_text(_("no answer"))
                    self.mode_label.set_text(
                        (_("The OpenAlloy service is not answering: {error}")
                         if backend.kind == "daemon" else
                         _("OpenAlloy is not answering: {error}")).format(error=err))
            def fetch():
                try:
                    return backend.call("status")
                finally:
                    self._poll_busy = False    # freed even if the reply is dropped
            self._async(fetch, done)
            return True

        def _status(self, st):
            prev = self.status.get("layout")
            self.status.update(st)
            dev = st.get("device") or {}
            if st.get("connected"):
                drv = dev.get("driver")
                self.conn_label.set_text((_("simulated") if dev.get("simulated") else
                                          _("connected"))
                                         + (" · " + _(SHORT_DRIVER[drv]) if drv in SHORT_DRIVER
                                            else ""))
                self.subtitle.set_text(f"v{__version__} — {dev.get('name', '')}")
            else:
                self.conn_label.set_text(_("disconnected"))
            if st.get("layout") and st["layout"] != prev:
                self.view.set_layout(get_layout(st["layout"]))
            self._mode_label()
            self._sync_buttons()

        def _mode_label(self):
            st = self.status
            eff = _(self._effect()["label"]) if self.effects else ""
            if not st.get("connected"):
                self.mode_label.set_text(_("Disconnected: {reason}").format(
                    reason=st.get("error") or _("waiting for the keyboard")))
                self.view.dimmed_text = _("Keyboard not connected")
            elif st.get("mode") == "fixed":
                self.mode_label.set_text(_("Fixed on the keyboard: {effect} (nothing streaming)")
                                         .format(effect=eff))
                self.view.dimmed_text = None
            elif st.get("mode") == "live":
                self.mode_label.set_text(_("Live: {effect}").format(effect=eff))
                self.view.dimmed_text = None
            else:
                self.mode_label.set_text(_("The keyboard's own lighting"))
                self.view.dimmed_text = _("The keyboard's own lighting")
            self.view.queue_draw()

        def _on_event(self, kind, data):
            if kind in ("status", "connected", "disconnected"):
                self._poll()
                if kind == "connected":
                    self._refresh_device()
            elif kind == "mode":
                self.status["mode"] = data.get("mode")
                self._mode_label()
                self._sync_buttons()
            elif kind == "flash":
                self._refresh_flash()
            elif kind == "profile_key":
                self.msg.set_text(_("Fn + {profile} pressed on the keyboard.").format(
                    profile=data.get("profile")))
            elif kind == "lighting" and not self._testing and data != self.applied:
                self.applied = data
                if data != self.lighting:
                    self.lighting = dict(data)
                    self._show_lighting()

        def _on_close(self, *_args):
            self._closing = True
            self._log_stop.set()
            if getattr(self, "_log_handler", None) is not None:
                logging.getLogger().removeHandler(self._log_handler)
            if self._log_proc:
                self._log_proc.terminate()
            return False

    class App(Gtk.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID + (".sim" if simulate else ""),
                             flags=Gio.ApplicationFlags.FLAGS_NONE)

        def do_startup(self):
            Gtk.Application.do_startup(self)
            provider = Gtk.CssProvider()
            provider.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        def do_activate(self):
            (self.props.active_window or Window(self)).present()

    return App()
