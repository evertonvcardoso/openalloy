# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""openalloy - command line interface.

Grown from Linuity's flag-based CLI into subcommands, because a keyboard has
far more to configure than a microphone ring. Every command works against the
running daemon, or in-process when the daemon is not running.

Every message is English in the source and translated through openalloy.i18n
(``--lang``, ``$OPENALLOY_LANG`` or the saved ``language`` setting).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from openalloy import __version__, i18n
from openalloy.application.backend import Backend, connect
from openalloy.config.settings import socket_path
from openalloy.domain.colors import DEFAULT_LED_GAMMA, led_correct, parse_color, to_hex
from openalloy.domain.layout import GROUPS, get_layout
from openalloy.i18n import _, format_percent, ngettext
from openalloy.infra.logging_config import setup_logging
from openalloy.infra.rpc import RpcError

UDEV_RULE_NAME = "60-openalloy.rules"
RES = Path(__file__).resolve().parent.parent.parent / "resources"

GREEN, YELLOW, RED, DIM, BOLD, RESET = ("\033[32m", "\033[33m", "\033[31m", "\033[2m",
                                        "\033[1m", "\033[0m")
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    GREEN = YELLOW = RED = DIM = BOLD = RESET = ""

YES_ANSWERS = ("y", "yes", "s", "sim")


def out(obj: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False))


def row(label: str, value: str) -> None:
    """One aligned 'Label : value' line (labels change width between languages)."""
    print(f"  {label:<12}: {value}")


def error(msg: str) -> None:
    print(f"{RED}{_('Error')}{RESET}: {msg}", file=sys.stderr)


def parse_params(items: List[str]) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(_("invalid parameter {item!r}; use name=value").format(item=item))
        k, v = item.split("=", 1)
        params[k.strip()] = v.strip()
    return params


def parse_keys(items: List[str], variant: str) -> Dict[str, str]:
    """--key w=#ff0000  --key wasd=red  --key 19=00ff00"""
    layout = get_layout(variant)
    keys: Dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(_("invalid --key {item!r}; use key=color").format(item=item))
        ref, col = item.split("=", 1)
        color = to_hex(parse_color(col))
        names = GROUPS.get(ref.lower(), [ref])
        for name in names:
            idx = layout.resolve(name)
            if idx is None or layout.key(idx) is None:
                raise SystemExit(_("unknown key: {key!r}").format(key=name))
            keys[str(idx)] = color
    return keys


def parse_level(text: str, current: int) -> int:
    t = text.strip()
    if t in ("+", "-"):
        from openalloy.infra.protocol import BRIGHTNESS_STEPS as steps
        i = min(range(len(steps)), key=lambda n: abs(steps[n] - current))
        return steps[min(len(steps) - 1, i + 1)] if t == "+" else steps[max(0, i - 1)]
    if t.endswith("%"):
        return round(int(t[:-1]) * 255 / 100)
    return int(t, 0)


_last_pct = [-1]


def progress_bar(fraction: float, message: str) -> None:
    pct = int(fraction * 100)
    if pct == _last_pct[0] and fraction < 1.0:
        return
    _last_pct[0] = pct
    if not sys.stderr.isatty():
        if fraction >= 1.0 or pct % 25 == 0:
            sys.stderr.write(f"  {format_percent(fraction):>5}  {message}\n")
        return
    width = 30
    filled = int(width * fraction)
    sys.stderr.write("\r  [%s%s] %5s  %-40s" % ("#" * filled, "." * (width - filled),
                                                format_percent(fraction), message[:40]))
    sys.stderr.flush()
    if fraction >= 1.0:
        sys.stderr.write("\n")


def ask_yes(question: str) -> bool:
    return input(f"  {question} [{_('y/N')}] ").strip().lower() in YES_ANSWERS


DRIVER_NAMES = {"hid_lamparray": i18n.N_("HID LampArray / Dynamic Lighting (interface 3)"),
                "hyperx_alloy_origins_core": i18n.N_("vendor channel (interface 2)")}


# --- commands ---------------------------------------------------------------

def cmd_status(be: Backend, a) -> None:
    st = be.call("status")
    if a.json:
        return out(st, True)
    dev = st.get("device") or {}
    print(f"{BOLD}OpenAlloy {__version__}{RESET}  {DIM}("
          + _("backend: {kind}").format(kind=be.kind) + f"){RESET}")
    if st["connected"]:
        sim = " " + _("(simulated)") if dev.get("simulated") else ""
        row(_("Keyboard"), f"{GREEN}{dev.get('name')}{RESET} [{dev.get('usb_id')}]{sim}")
        if dev.get("variant") and dev.get("variant") != st["layout"]:
            origin = _("set manually; the keyboard reports {layout}").format(
                layout=dev["variant"].upper())
        elif dev.get("variant_byte") is not None:
            origin = _("byte 0x{value:02x}").format(value=dev["variant_byte"])
        else:
            origin = _("detected from the LEDs")
        row(_("Firmware"), f"{dev.get('firmware')}   " + _("Layout: {layout} ({origin})").format(
            layout=st["layout"].upper(), origin=origin))
        drv = dev.get("driver")
        row(_("Driver"), _(DRIVER_NAMES[drv]) if drv in DRIVER_NAMES else str(drv))
        try:
            ds = be.call("device_state")
            prof = ds.get("profile") if ds.get("profile") is not None else "-"
            soft = " " + _("(software)") if drv == "hid_lamparray" else ""
            row(_("Profile"), f"{prof}   " + _("Brightness: {level}/255").format(
                level=ds.get("brightness")) + soft)
        except RpcError:
            pass
    else:
        row(_("Keyboard"), f"{RED}{_('disconnected')}{RESET}  {st.get('error') or ''}")
    light = st["lighting"]
    if st["mode"] == "live":
        mode = _("live (software)")
    elif st["mode"] == "fixed":
        mode = (_("fixed color on the keyboard (nothing streaming)") if st.get("fixed_applied")
                else _("fixed color on the keyboard (nothing streaming), applying..."))
    elif st["mode"] == "onboard":
        mode = _("the keyboard's own lighting (onboard memory)")
    else:
        mode = st["mode"]
    row(_("Mode"), mode)
    row(_("Effect"), f"{light['effect']}  {DIM}{json.dumps(light['params'], ensure_ascii=False)}"
                     f"{RESET}")
    if light.get("keys"):
        n = len(light["keys"])
        row(_("Keys"), ngettext("{n} with a custom color", "{n} with a custom color", n)
            .format(n=n))
    try:
        fs = be.call("flash_status")
        mark = f"{GREEN}{_('unlocked')}{RESET}" if fs["allowed"] else f"{YELLOW}{_('blocked')}{RESET}"
        row(_("Memory write"), f"{mark} {DIM}({fs['reason']}){RESET}")
    except RpcError:
        pass


def cmd_effects(be: Backend, a) -> None:
    effects = be.call("effects")
    if a.json:
        return out(effects, True)
    for e in effects:
        tag = (f" {YELLOW}[{_('system sound')}]{RESET}" if e.get("audio") else
               f" {YELLOW}[{_('live mode only')}]{RESET}" if e["live_only"] else "")
        print(f"{BOLD}{e['id']:<11}{RESET} {_(e['label'])}{tag}\n"
              f"            {DIM}{_(e['description'])}{RESET}")
        for p in e["params"]:
            rng = ""
            if p["choices"]:
                rng = " (" + "|".join(p["choices"]) + ")"
            elif p["min"] is not None:
                rng = f" ({p['min']:g}-{p['max']:g})"
            print(f"              {p['name']}={p['default']}{rng}  {DIM}{_(p['label'])}{RESET}")


def _lighting_from_args(be: Backend, a) -> Dict[str, Any]:
    st = be.call("status")
    params = parse_params(a.param)
    for name in ("color", "colors", "speed", "direction"):
        val = getattr(a, name, None)
        if val is not None:
            params[name] = val
    keys = parse_keys(a.key, st["layout"])
    if getattr(a, "keep_keys", False):
        keys = dict(st["lighting"].get("keys") or {}, **keys)
    reaction = dict(st["lighting"].get("reaction") or {}) if getattr(a, "keep_keys", False) else {}
    if getattr(a, "react", None):
        reaction["type"] = a.react
    if getattr(a, "react_color", None):
        if a.react_color.lower() in ("rainbow", "arco-iris", "arco-íris"):
            reaction["rainbow"] = True
        else:
            reaction["color"] = a.react_color
            reaction["rainbow"] = False
    return {"effect": a.effect, "params": params, "keys": keys, "reaction": reaction}


def cmd_apply(be: Backend, a) -> None:
    light = be.call("apply", lighting=_lighting_from_args(be, a), persist=not a.no_save)
    if a.json:
        return out(light, True)
    print(f"{GREEN}{_('Applied')}{RESET}: {light['effect']} "
          f"{DIM}{json.dumps(light['params'], ensure_ascii=False)}{RESET}")
    if be.kind == "local":
        print(f"{YELLOW}{_('Warning')}{RESET}: "
              + _("the service is not running; the live effect stops when this command ends.\n"
                  "  Start the service: openalloy service enable  (or write it to memory: "
                  "openalloy onboard write 1)"))
        _hold(be)


def _hold(be: Backend) -> None:
    import time
    try:
        print(f"{DIM}  {_('Keeping the effect running... Ctrl+C to quit.')}{RESET}")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print()


def cmd_brightness(be: Backend, a) -> None:
    if a.level is None:
        level = be.call("get_brightness")
        if a.json:
            print(json.dumps(level))
        elif level is None:
            print(_("Brightness: unknown (the keyboard did not report it)"))
        else:
            print(_("Brightness: {level}/255 ({percent})").format(
                level=level, percent=format_percent(level / 255)))
        return
    current = be.call("get_brightness") or 0
    level = max(0, min(255, parse_level(a.level, current)))
    got = be.call("set_brightness", level=level)
    print(got if a.json else _("Brightness: {level}/255").format(level=got))


def cmd_profile(be: Backend, a) -> None:
    if a.number is None:
        p = be.call("get_profile")
        print(p if a.json else _("Active profile: {profile}").format(profile=p))
        return
    got = be.call("set_profile", profile=a.number)
    be.call("release")
    print(got if a.json else _("Active profile: {profile} (onboard memory mode)").format(profile=got))


def cmd_onboard(be: Backend, a) -> None:
    if a.action == "read":
        profiles = [a.profile] if a.profile else [1, 2, 3]
        result = {}
        for p in profiles:
            cols = be.call("read_onboard", profile=p)
            result[p] = cols
            if not a.json:
                counts: Dict[str, int] = {}
                st = be.call("status")
                layout = get_layout(st["layout"])
                for k in layout.keys:
                    counts[cols[k.index]] = counts.get(cols[k.index], 0) + 1
                top = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
                print(_("Profile {profile} - key-press layer: {colors}").format(
                    profile=p, colors=", ".join(f"{n}x {c}" for c, n in top)))
        if a.json:
            out(result, True)
        else:
            print(f"{DIM}"
                  + _("The base animation cannot be read back (the firmware has no read for 0xA5).")
                  + RESET)
        return

    if a.action == "write":
        if not a.profile:
            raise SystemExit(_("give the profile: openalloy onboard write 1|2|3 [EFFECT]"))
        lighting = _lighting_from_args(be, a) if a.effect else None
        if lighting is None:
            lighting = be.call("status")["lighting"]
        keypress = a.keypress
        kp_color = None
        if keypress not in ("match", "black", "keep"):
            kp_color = to_hex(parse_color(keypress))
            keypress = "custom"
        if not a.yes:
            print(f"{YELLOW}" + _("This writes to the keyboard's flash memory (profile {profile}).")
                  .format(profile=a.profile) + f"{RESET}\n  "
                  + _("The current content of that profile will be replaced. Sequence validated on\n"
                      "  firmware 21.08; use at your own risk (see the README)."))
            if not ask_yes(_("Continue?")):
                raise SystemExit(_("cancelled"))
        res = be.call("upload_onboard", profile=a.profile, lighting=lighting, frames=a.frames,
                      keypress=keypress, keypress_color=kp_color,
                      progress=None if a.json else progress_bar)
        if a.json:
            return out(res, True)
        ver = {True: _("verified"), False: _("MISMATCH"), None: _("kept")}[res["keypress_verified"]]
        frames = ngettext("{n} frame", "{n} frames", res["frames"]).format(n=res["frames"])
        print(f"{GREEN}" + _("Profile {profile} written").format(profile=res["profile"])
              + f"{RESET}: " + _("{effect}, {frames}; key-press layer {mode} ({check}).").format(
                  effect=res["lighting"]["effect"], frames=frames, mode=res["keypress"], check=ver)
              + "\n  " + _("The effect now runs on the keyboard itself, with no service, even on "
                           "another computer."))


def cmd_preset(be: Backend, a) -> None:
    if a.action != "list" and not a.name:
        raise SystemExit((_("give the name: openalloy preset {action} NAME FILE")
                          if a.action == "export" else
                          _("give the name: openalloy preset {action} NAME")).format(action=a.action))
    if a.action == "export" and not a.file:
        raise SystemExit(_("give the file: openalloy preset export NAME FILE.json"))
    if a.action == "list":
        presets = be.call("presets_list")
        if a.json:
            return out(presets, True)
        for p in presets:
            tag = f"{DIM}({_('built-in')}){RESET}" if p["builtin"] else ""
            shown = i18n.pgettext("preset", p["name"]) if p["builtin"] else p["name"]
            print(f"  {p['id']:<20} {shown:<28} {p['lighting']['effect']:<11} {tag}")
        return
    if a.action == "apply":
        light = be.call("preset_apply", ident=a.name)
        print(f"{GREEN}{_('Preset applied')}{RESET}: {a.name} ({light['effect']})")
        if be.kind == "local":
            _hold(be)
        return
    if a.action == "save":
        ident = be.call("preset_save", name=a.name, lighting=be.call("status")["lighting"])
        print(_("Saved as {name}").format(name=ident))
        return
    if a.action == "delete":
        if not be.call("preset_delete", ident=a.name):
            raise ValueError(_("preset not found: {name}").format(name=a.name))
        print(_("Removed"))
        return
    if a.action == "export":
        p = next((x for x in be.call("presets_list") if x["id"] == a.name or x["name"] == a.name),
                 None)
        if not p:
            raise SystemExit(_("preset not found: {name}").format(name=a.name))
        Path(a.file).write_text(json.dumps({"name": p["name"], "lighting": p["lighting"]},
                                           indent=2, ensure_ascii=False))
        print(_("Exported to {file}").format(file=a.file))
        return
    if a.action == "import":
        data = json.loads(Path(a.name).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise SystemExit(_("invalid preset file (a JSON object is expected)"))
        name = data.get("name") or Path(a.name).stem
        ident = be.call("preset_save", name=name, lighting=data.get("lighting", data))
        print(_("Imported as {name}").format(name=ident))


def cmd_test_rgb(be: Backend, a) -> None:
    """Show a known pattern; the user's answer unlocks (or not) flash writes."""
    res = be.call("live_test")
    print(f"{BOLD}{_('Live RGB test')}{RESET} - " + _("look at the physical keyboard."))
    print("  " + _("Expected: {pattern}").format(pattern=res["expected"]))
    if a.answer is None:
        try:
            ok = ask_yes(_("Do the keys show exactly that?"))
        except EOFError:
            ok = False
        except KeyboardInterrupt:
            be.call("confirm_live_test", ok=False)      # put the lighting back
            raise
    else:
        ok = a.answer == "yes"
    st = be.call("confirm_live_test", ok=ok)
    if ok:
        state = _("unlocked") if st["allowed"] else _("blocked")
        print(f"{GREEN}{_('Confirmed.')}{RESET} " + _("Writing to memory: {state} ({reason}).")
              .format(state=state, reason=st["reason"]))
    else:
        print(f"{YELLOW}{_('Live RGB did not work on this keyboard.')}{RESET} "
              + _("Writing to memory stays blocked.\n  Run 'openalloy doctor', close OpenRGB and "
                  "check the udev rule; send the output for analysis."))


def cmd_release(be: Backend, a) -> None:
    be.call("release")
    print(_("Onboard memory mode: the keyboard shows the profile stored in it."))


def cmd_resume(be: Backend, a) -> None:
    be.call("resume")
    print(_("Live mode resumed."))


def cmd_settings(be: Backend, a) -> None:
    if a.set:
        values = {}
        for k, v in parse_params(a.set).items():
            try:
                values[k] = json.loads(v)
            except ValueError:
                values[k] = v
        be.call("settings_update", **values)
    s = be.call("settings_get")
    print(json.dumps(s, indent=2, ensure_ascii=False))


def cmd_language(a) -> int:
    """Show or save the language. Talks to the service when it runs (it switches too);
    otherwise writes the setting directly - never starts an engine just for this."""
    from openalloy.config.settings import Settings
    from openalloy.infra.rpc import is_alive
    remote = None
    if is_alive(socket_path()):
        from openalloy.application.backend import RemoteBackend
        remote = RemoteBackend()
    if a.code:
        code = i18n.normalize(a.code)
        if code is None:
            raise SystemExit(_("unknown language {code!r}; available: {codes}").format(
                code=a.code, codes=", ".join(i18n.LANGUAGES)))
        if remote is not None:
            remote.call("settings_update", language=code)
        else:
            Settings().update(language=code)
        i18n.set_language(code)
        print(_("Language: {name}").format(name=i18n.LANGUAGES[code]))
        return 0
    current = (remote.call("settings_get") if remote else Settings().snapshot()).get("language") \
        or i18n.DEFAULT
    for code, name in i18n.LANGUAGES.items():
        print(f"  {'*' if code == current else ' '} {code:<6} {name}")
    return 0


# --- commands that do not need a backend -------------------------------------

def udev_rule_text() -> str:
    return (RES / UDEV_RULE_NAME).read_text()


def cmd_udev(a) -> int:
    print(udev_rule_text(), end="")
    return 0


def cmd_service(a) -> int:
    if not shutil.which("systemctl"):
        print(_("systemctl not found"))
        return 1
    verb = {"enable": ["enable", "--now"], "disable": ["disable", "--now"]}.get(a.action, [a.action])
    return subprocess.call(["systemctl", "--user", *verb, "openalloy.service"])


def cmd_doctor(a) -> int:
    from openalloy.infra import protocol as P
    from openalloy.infra.drivers import find_lamparray_nodes, find_nodes, hyperx_nodes
    from openalloy.infra.input import find_event_nodes
    from openalloy.infra.lamparray import USAGE_PAGE_LIGHTING
    from openalloy.infra.rpc import is_alive

    ok = True

    def line(state, text):
        mark = {True: f"{GREEN}{_('OK')}{RESET}", False: f"{RED}{_('ERROR')}{RESET}",
                None: f"{YELLOW}{_('WARNING')}{RESET}"}[state]
        print(f"  [{mark}] {text}")

    print(f"{BOLD}" + _("OpenAlloy {version} diagnostics").format(version=__version__) + RESET)
    line(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]}")
    all_nodes = hyperx_nodes()
    nodes = find_nodes()
    print("  " + _("Keyboard USB interfaces (read-only query, nothing is sent):"))
    for node in all_nodes:
        if (node.vendor_id, node.product_id) in P.SUPPORTED_IDS:
            access = "rw" if os.access(node.path, os.R_OK | os.W_OK) else _("no rw access")
            if USAGE_PAGE_LIGHTING in node.usage_pages:
                role = "LampArray/Dynamic Lighting"
            elif P.CONTROL_USAGE_PAGE in node.usage_pages or node.interface == P.CONTROL_INTERFACE:
                role = _("vendor control")
            else:
                role = _("keys/media")
            print(f"      {node.vendor_id:04x}:{node.product_id:04x} if{node.interface} "
                  f"{node.path} ({role}, {access})")
    line(None, _("Being identified does not guarantee the firmware accepts RGB commands: "
                 "run 'openalloy test-rgb'"))
    lamps = find_lamparray_nodes()
    usable = [n for _c, n in nodes] + [n for n in lamps if n not in [m for _c, m in nodes]]
    if usable:
        node = usable[0]
        line(True, _("Keyboard found: {usb} ({interfaces})").format(
            usb=f"{node.vendor_id:04x}:{node.product_id:04x}",
            interfaces=", ".join(f"if{n.interface}" for n in usable)))
        granted = []
        for n in usable:
            try:
                os.close(os.open(n.path, os.O_RDWR))
                granted.append(n)
            except OSError:
                pass
        if granted:
            line(True, _("Read/write permission on {nodes}").format(
                nodes=", ".join(f"{n.path} (if{n.interface})" for n in granted)))
        else:
            ok = False
            line(False, _("No permission on {nodes}. Install the rule: {command} and replug the "
                          "keyboard").format(
                nodes=", ".join(n.path for n in usable),
                command=f"openalloy udev-rules | sudo tee /etc/udev/rules.d/{UDEV_RULE_NAME}"))
    else:
        ok = False
        others = ", ".join(f"{n.vendor_id:04x}:{n.product_id:04x}/if{n.interface}" for n in all_nodes)
        line(False, (_("Alloy Origins Core (0951:16e6 or 03f0:098f) not found; other HyperX: "
                       "{devices}").format(devices=others) if others else
                     _("Alloy Origins Core (0951:16e6 or 03f0:098f) not found")))
    rule = [d for d in ("/etc/udev/rules.d", "/usr/lib/udev/rules.d")
            if os.path.exists(os.path.join(d, UDEV_RULE_NAME))]
    line(bool(rule) or None, _("udev rule in {path}").format(path=rule[0]) if rule
         else _("udev rule not installed"))
    alive = is_alive(socket_path())
    line(alive or None, _("Service running at {path}").format(path=socket_path()) if alive
         else _("Service stopped"))
    unit = Path.home() / ".config/systemd/user/openalloy.service"
    line(unit.exists() or None, _("systemd user service installed") if unit.exists()
         else _("systemd user service not installed"))
    ev = find_event_nodes(P.SUPPORTED_IDS)
    readable = [n for n in ev if os.access(n, os.R_OK)]
    line(bool(readable) or None, _("Reading keys for reactive effects: available") if readable
         else _("Reading keys for reactive effects: unavailable (optional: install.sh --reactive "
                "or the input group)"))
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk  # noqa: F401
        line(True, _("GTK4 / PyGObject for the graphical interface"))
    except Exception:
        line(None, _("GTK4/PyGObject unavailable (the window will not open; the CLI works)"))
    for proc in ("openrgb", "ckb-next"):
        if shutil.which("pgrep") and subprocess.call(["pgrep", "-x", proc],
                                                     stdout=subprocess.DEVNULL) == 0:
            line(None, _("{program} is running and may fight over the keyboard").format(program=proc))
    from openalloy.application.flashgate import ENV, ENV_VALUE
    from openalloy.config.settings import Settings
    validated = Settings().get("flash_validated") or {}
    if os.environ.get(ENV) == ENV_VALUE:
        line(None, _("Writing to memory unlocked by the {variable} variable").format(variable=ENV))
    elif validated:
        line(True, _("Writing to memory unlocked for: {devices}").format(
            devices=", ".join(sorted(validated))))
    else:
        line(None, _("Writing to memory blocked until live RGB is confirmed (openalloy test-rgb)"))
    return 0 if ok else 1


def cmd_hold(a) -> int:
    """Set a colour and exit without releasing the LEDs - nothing keeps running."""
    import time

    from openalloy.application.effects import Lighting, Renderer, normalize
    from openalloy.domain.layout import get_layout
    from openalloy.infra.drivers import open_driver
    from openalloy.infra.rpc import is_alive

    if is_alive(socket_path()) and not a.stop_service:
        # Service running: let it hold the colour (it re-applies after replug/wake).
        from openalloy.application.backend import RemoteBackend
        be = RemoteBackend()
        if a.release:
            be.call("release")
            print(_("LEDs handed back to the keyboard."))
            return 0
        st = be.call("status")
        layout = get_layout(st.get("layout") or "ansi")
        light = ({"effect": "static", "params": {"color": a.color}} if a.color else None)
        if a.preset:
            from openalloy.config.settings import PresetStore
            p = PresetStore().get(a.preset)
            if not p:
                raise SystemExit(_("preset not found: {name}").format(name=a.preset))
            light = dict(p["lighting"])
        if light is None:
            raise SystemExit(_("give a color (e.g. openalloy hold '#ff0000') or --preset NAME"))
        light["keys"] = dict(light.get("keys") or {}, **parse_keys(a.key, layout.variant))
        if a.brightness is not None:
            be.call("set_brightness", level=max(0, min(255, parse_level(a.brightness, 255))))
        be.call("fix", lighting=light)
        st = {}
        for _attempt in range(40):               # wait until the service applied it
            st = be.call("status")
            if st.get("fixed_applied") or st.get("mode") != "fixed":
                break
            time.sleep(0.1)
        if st.get("fixed_applied"):
            print(f"{GREEN}{_('Color fixed by the service')}{RESET}: "
                  + _("it stays on the keyboard with nothing streaming and is re-applied when "
                      "the keyboard is replugged or the PC wakes up."))
        elif (st.get("device") or {}).get("driver") == "hyperx_alloy_origins_core":
            print(f"{YELLOW}{_('This keyboard uses the vendor channel')}{RESET}: "
                  + _("the service keeps the color by sending it continuously."))
        else:
            print(f"{YELLOW}{_('Color saved')}{RESET}, "
                  + _("but the keyboard has not confirmed it yet: {reason}. The service applies "
                      "it as soon as it can.").format(
                      reason=st.get("error") or _("waiting for the keyboard")))
        return 0
    if is_alive(socket_path()):
        subprocess.call(["systemctl", "--user", "stop", "openalloy.service"])
        time.sleep(1.0)

    simulator = None
    if a.simulate == "hp":
        from openalloy.infra.simulator import SimulatedLampArray
        simulator = SimulatedLampArray()
    dev = open_driver(a.simulate or False, simulator)
    info = dev.info()

    if a.release:
        dev.release_control()
        dev.close()
        print(_("LEDs handed back to the keyboard: it shows its own lighting again."))
        return 0

    layout = get_layout(info.variant or "ansi")
    if a.preset:
        from openalloy.config.settings import PresetStore
        p = PresetStore().get(a.preset)
        if not p:
            raise SystemExit(_("preset not found: {name}").format(name=a.preset))
        light = Lighting.from_dict(p["lighting"])
    else:
        if not a.color:
            raise SystemExit(_("give a color (e.g. openalloy hold '#ff0000') or --preset NAME"))
        light = Lighting("static", {"color": a.color})
    keys = parse_keys(a.key, layout.variant)
    light.keys.update({int(k): parse_color(v) for k, v in keys.items()})
    light.reaction = {}
    light = normalize(light, layout)
    renderer = Renderer(light, layout, live=False)
    if renderer.animated:
        print(f"{YELLOW}{_('Warning')}{RESET}: "
              + _("'{effect}' is animated; with nothing running it stays on its first frame.")
              .format(effect=light.effect))
    if a.brightness is not None:
        dev.set_brightness(max(0, min(255, parse_level(a.brightness, 255))))
    from openalloy.config.settings import Settings
    frame = led_correct(renderer.frame(0.0), Settings().get("led_gamma", DEFAULT_LED_GAMMA))
    # Send twice, a moment apart, so a slow firmware certainly latched it.
    dev.stream(frame)
    time.sleep(0.15)
    dev.stream(frame)

    detach = getattr(dev, "detach", None)
    if detach is None:
        dev.close()
        print(f"{YELLOW}{_('This keyboard uses the vendor channel')}{RESET}, "
              + _("which goes back to its stored profile within a few seconds.\n  For a fixed "
                  "color with no software on it, use: openalloy onboard write 1 static -c COLOR"))
        return 0
    detach()
    if simulator is not None:
        print(_("(simulated) host control kept: {kept}").format(
            kept=_("yes") if not simulator.autonomous else _("no")))
    print(f"{GREEN}{_('Color applied and kept')}{RESET}; "
          + _("OpenAlloy has exited and nothing is left running.\n  It lasts until the keyboard "
              "restarts (unplugging the cable, rebooting or\n  suspending the PC). To undo: "
              "openalloy hold --release"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="openalloy",
                                 description=_("HyperX Alloy Origins Core configurator for Linux"))
    ap.add_argument("--version", action="version", version=f"OpenAlloy {__version__}",
                    help=_("show the version and exit"))
    ap.add_argument("--lang", metavar=_("CODE"),
                    help=_("interface language for this command: {codes}").format(
                        codes=", ".join(i18n.LANGUAGES)))
    ap.add_argument("--simulate", action="store_const", const="core",
                    help=_("use a simulated keyboard (no hardware)"))
    ap.add_argument("--simulate-hp", dest="simulate", action="store_const", const="hp",
                    help=_("simulate the HP 03f0:098f fw 21.16 (HID LampArray only)"))
    ap.add_argument("--sim-layout", choices=["ansi", "abnt2", "iso"],
                    help=_("layout of the simulated keyboard: ansi (US), abnt2 (default) or iso"))
    ap.add_argument("--json", action="store_true", help=_("JSON output"))
    ap.add_argument("-v", "--verbose", action="store_true", help=_("more detailed log"))
    sub = ap.add_subparsers(dest="cmd", title=_("commands"), metavar=_("COMMAND"))

    sub.add_parser("status", help=_("keyboard and effect state"))
    sub.add_parser("effects", help=_("list effects and parameters"))

    def lighting_args(p, effect_required=True):
        if effect_required:
            p.add_argument("effect", help=_("effect id (see 'openalloy effects')"))
        else:
            p.add_argument("effect", nargs="?", help=_("effect (default: the current one)"))
        p.add_argument("-p", "--param", action="append", default=[], metavar=_("NAME=VALUE"),
                       help=_("effect parameter"))
        p.add_argument("-c", "--color", help=_("shortcut for -p color="))
        p.add_argument("--colors", help=_("shortcut for -p colors=  (e.g. red,#00ff00,blue)"))
        p.add_argument("-s", "--speed", help=_("shortcut for -p speed="))
        p.add_argument("-d", "--direction", help=_("shortcut for -p direction="))
        p.add_argument("-k", "--key", action="append", default=[], metavar=_("KEY=COLOR"),
                       help=_("fixed color per key; accepts a name, an index or a group ({groups})")
                       .format(groups=", ".join(GROUPS)))
        p.add_argument("--keep-keys", action="store_true", help=_("keep the current per-key colors"))
        p.add_argument("-r", "--react", choices=["none", "flash", "ripple"],
                       help=_("key-press reaction layer on top of the effect (live)"))
        p.add_argument("--react-color", help=_("reaction color (or 'rainbow')"))

    p = sub.add_parser("apply", help=_("apply an effect live"))
    lighting_args(p)
    p.add_argument("--no-save", action="store_true", help=_("do not save it in the settings"))

    p = sub.add_parser("brightness", help=_("read or set the brightness"))
    p.add_argument("level", nargs="?", help=_("0-255, N%%, + or -"))

    p = sub.add_parser("profile", help=_("read or select the onboard profile (1-3)"))
    p.add_argument("number", nargs="?", type=int, choices=[1, 2, 3])

    p = sub.add_parser("onboard", help=_("keyboard memory: read/write profiles"))
    p.add_argument("action", choices=["read", "write"])
    p.add_argument("profile", nargs="?", type=int, choices=[1, 2, 3])
    lighting_args(p, effect_required=False)
    p.add_argument("--frames", type=int, help=_("animation frames (1-255)"))
    p.add_argument("--keypress", default="match",
                   help=_("key-press layer: match | black | keep | #color"))
    p.add_argument("-y", "--yes", action="store_true", help=_("do not ask for confirmation"))

    p = sub.add_parser("preset", help=_("saved presets"))
    p.add_argument("action", choices=["list", "apply", "save", "delete", "export", "import"])
    p.add_argument("name", nargs="?")
    p.add_argument("file", nargs="?")

    p = sub.add_parser("hold", help=_("set a color and exit with nothing running (fixed color)"))
    p.add_argument("color", nargs="?", help=_("color, e.g. '#ff0000' or red"))
    p.add_argument("-k", "--key", action="append", default=[], metavar=_("KEY=COLOR"),
                   help=_("color per key/group, e.g. -k wasd=white"))
    p.add_argument("--preset", help=_("use a preset (animated effects stay still)"))
    p.add_argument("-b", "--brightness", help=_("0-255 or N%%"))
    p.add_argument("--release", action="store_true", help=_("hand the LEDs back to the keyboard"))
    p.add_argument("--stop-service", action="store_true",
                   help=_("stop the openalloy service first (it would take the keyboard back)"))
    p = sub.add_parser("test-rgb", help=_("test live RGB and unlock writing to memory"))
    g = p.add_mutually_exclusive_group()
    g.add_argument("--yes", dest="answer", action="store_const", const="yes",
                   help=_("answer 'it worked' without asking"))
    g.add_argument("--no", dest="answer", action="store_const", const="no",
                   help=_("answer 'it did not work' without asking"))
    sub.add_parser("release", help=_("stop live mode and show the onboard profile"))
    sub.add_parser("resume", help=_("go back to live mode"))
    p = sub.add_parser("settings", help=_("show/change settings"))
    p.add_argument("--set", action="append", default=[], metavar=_("KEY=VALUE"),
                   help=_("change a setting"))
    p = sub.add_parser("language", help=_("show or choose the interface language"))
    p.add_argument("code", nargs="?", help=", ".join(i18n.LANGUAGES))

    sub.add_parser("gui", help=_("open the graphical interface"))
    sub.add_parser("daemon", help=_("run the service in the foreground"))
    sub.add_parser("doctor", help=_("installation and permission diagnostics"))
    sub.add_parser("udev-rules", help=_("print the udev rule"))
    p = sub.add_parser("service", help=_("control the systemd user service"))
    p.add_argument("action", choices=["start", "stop", "restart", "status", "enable", "disable"])
    return ap


HANDLERS = {
    "status": cmd_status, "effects": cmd_effects, "apply": cmd_apply,
    "brightness": cmd_brightness, "profile": cmd_profile, "onboard": cmd_onboard,
    "preset": cmd_preset, "release": cmd_release, "resume": cmd_resume,
    "test-rgb": cmd_test_rgb, "settings": cmd_settings,
}


def _early_language(argv) -> None:
    """Pick the language before building the parser, so --help is translated too."""
    lang = None
    for i, arg in enumerate(argv):               # errors are left to the real parser
        if arg == "--":
            break
        if arg == "--lang" and i + 1 < len(argv):
            lang = argv[i + 1]
        elif arg.startswith("--lang="):
            lang = arg.split("=", 1)[1]
    i18n.set_language(i18n.resolve(lang))
    i18n.install_argparse()


def main(argv=None) -> int:
    """Entry point. A reader that goes away (``openalloy status | head -1``) is not an
    error: the rest of the output is discarded quietly, as other command-line tools do."""
    try:
        code = _main(argv)
        sys.stdout.flush()
        return code
    except BrokenPipeError:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())    # no second error at interpreter exit
            os.close(devnull)
        except (OSError, ValueError):
            pass
        return 0


def _main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    _early_language(argv)
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.sim_layout:
        os.environ["OPENALLOY_SIM_LAYOUT"] = a.sim_layout
    if a.lang:
        if i18n.normalize(a.lang) is None:
            print(f"{YELLOW}{_('Warning')}{RESET}: " + _(
                "unknown language {code!r}; available: {codes}").format(
                code=a.lang, codes=", ".join(i18n.LANGUAGES)), file=sys.stderr)
        os.environ[i18n.ENV] = i18n.current()       # a service started from here follows it
    setup_logging(verbose=a.verbose)
    if a.cmd is None:
        ap.print_help()
        return 0
    if a.cmd == "gui":
        from openalloy.presentation.gui.app import launch
        return launch(simulate=a.simulate)
    if a.cmd == "daemon":
        from openalloy.presentation.daemon.main import main as dmain
        flag = {"core": ["--simulate"], "hp": ["--simulate-hp"]}.get(a.simulate, [])
        return dmain(flag)
    if a.cmd == "doctor":
        return cmd_doctor(a)
    if a.cmd == "udev-rules":
        return cmd_udev(a)
    if a.cmd == "language":
        return cmd_language(a)
    if a.cmd == "service":
        return cmd_service(a)
    if a.cmd == "hold":
        try:
            return cmd_hold(a)
        except (OSError, ValueError, RpcError) as exc:
            if isinstance(exc, BrokenPipeError):
                raise
            error(str(exc))
            return 1
        except Exception as exc:
            if type(exc).__name__ in ("EngineError", "ProtocolError", "LampArrayError"):
                error(str(exc))
                return 1
            raise

    be = connect(simulate=a.simulate)
    try:
        if be.kind == "local":
            import time
            time.sleep(0.3)  # let the in-process engine connect
        HANDLERS[a.cmd](be, a)
        return 0
    except (RpcError, ValueError, OSError, RuntimeError) as exc:
        if isinstance(exc, BrokenPipeError):
            raise
        error(str(exc))
        return 1
    except Exception as exc:  # EngineError / ProtocolError in local mode
        if type(exc).__name__ in ("EngineError", "ProtocolError", "LampArrayError"):
            error(str(exc))
            return 1
        raise
    except KeyboardInterrupt:
        return 130
    finally:
        be.close()


if __name__ == "__main__":
    sys.exit(main())
