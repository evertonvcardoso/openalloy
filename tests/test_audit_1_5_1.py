# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Regression tests for the 1.5.1 audit (each failed on 1.5.0)."""
import ast
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from openalloy import i18n

ROOT = Path(__file__).resolve().parent.parent


# --- H1 / M1: "_" shadowed by a local variable --------------------------------------------

def _functions_shadowing_gettext(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        body = fn.body if isinstance(fn.body, list) else [fn.body]
        calls = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_"
                    for b in body for n in ast.walk(b))
        args = fn.args.args + fn.args.kwonlyargs + [a for a in (fn.args.vararg, fn.args.kwarg) if a]
        stores = any(a.arg == "_" for a in args) or any(
            (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id == "_")
            or (isinstance(n, ast.ExceptHandler) and n.name == "_")
            for b in body for n in ast.walk(b))
        if calls and stores:
            yield fn.lineno


def test_no_function_shadows_the_translation_function():
    """A local named "_" makes every _() in that function fail (UnboundLocalError or
    "'list' object is not callable"). It slipped into 1.5.0 twice."""
    files = [p for p in ROOT.rglob("*.py") if "locales" not in p.parts and ".git" not in p.parts]
    found = [f"{p.relative_to(ROOT)}:{ln}" for p in files for ln in _functions_shadowing_gettext(p)]
    assert found == []


def _transport_on(fd):
    from openalloy.infra.transport import HidrawTransport
    t = HidrawTransport.__new__(HidrawTransport)
    t.path, t._fd, t._lock = "/dev/hidrawX", fd, threading.Lock()
    return t


def test_failed_read_raises_transport_error():
    from openalloy.infra.transport import TransportError
    r, w = os.pipe()
    os.close(w)
    os.close(r)                         # the node vanished, like an unplugged keyboard
    with pytest.raises(TransportError, match="read from /dev/hidrawX failed"):
        _transport_on(r).read(0.0)


def test_read_on_closed_transport_raises_transport_error():
    from openalloy.infra.transport import TransportError
    with pytest.raises(TransportError, match="device closed"):
        _transport_on(None).read(0.0)


def test_failed_read_is_translated():
    from openalloy.infra.transport import TransportError
    i18n.set_language("pt_BR")
    with pytest.raises(TransportError, match="dispositivo fechado"):
        _transport_on(None).read(0.0)


def test_key_reader_thread_stops_cleanly(monkeypatch):
    from openalloy.infra import input as I
    errors = []
    monkeypatch.setattr(threading, "excepthook", lambda a: errors.append(a.exc_value))
    r, w = os.pipe()
    monkeypatch.setattr(I, "find_event_nodes", lambda ids: ["/dev/fake"])
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda p, fl, *a: r if p == "/dev/fake" else real_open(p, fl, *a))
    src = I.EvdevKeySource([], lambda code: None)
    src.start()
    time.sleep(0.6)
    assert src.status.startswith("active")
    src.stop()
    src._thread.join(2)
    os.close(w)
    assert errors == [] and src.status == "stopped"


# --- M2: a broken translation falls back to English ------------------------------------------

def test_translation_with_wrong_variables_falls_back_to_english(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "tools"))
    import i18n as T
    shutil.copytree(T.LOCALES, tmp_path / "locales")
    po = tmp_path / "locales/pt_BR/LC_MESSAGES/openalloy.po"
    header, entries = T.parse_po(po)
    for e in entries:
        if e.msgid == "Profile {profile} active on the keyboard.":
            e.msgstr = ["Perfil {perfil} ativo no teclado."]          # translator typo
    T.write_po(po, header, entries)
    (po.with_suffix(".mo")).write_bytes(T.build_mo(*T.parse_po(po)))
    monkeypatch.setattr(i18n, "LOCALEDIR", tmp_path / "locales")
    i18n.set_language("pt_BR")
    assert i18n._("Profile {profile} active on the keyboard.").format(profile=2) == \
        "Profile 2 active on the keyboard."
    assert i18n._("Apply") == "Aplicar"                # the rest stays translated


# --- M3: LampArray is not flooded with identical frames --------------------------------------

def _engine(tmp_path, opener):
    from openalloy.application.engine import Engine
    from openalloy.config.settings import PresetStore, Settings
    e = Engine(opener, Settings(tmp_path / "c.json"), PresetStore(tmp_path / "p"),
               enable_input=False)
    e.start()
    return e


def test_lamparray_idle_traffic_is_low_and_changes_are_immediate(tmp_path):
    from openalloy.infra.drivers import open_driver
    from openalloy.infra.simulator import SimulatedLampArray
    arr = SimulatedLampArray()
    e = _engine(tmp_path, lambda: open_driver("hp", arr))
    try:
        e.apply({"effect": "static", "params": {"color": "#ff0000"},
                 "keys": {"19": "#ffffff", "26": "#00ff00"}})
        time.sleep(0.5)
        n = len(arr.log)
        time.sleep(2.0)
        assert (len(arr.log) - n) / 2.0 <= 8           # was 48 reports/s in 1.5.0
        e.apply({"effect": "static", "params": {"color": "#0000ff"}})
        deadline = time.time() + 0.4
        while arr.key_colors()[30] != (0, 0, 255) and time.time() < deadline:
            time.sleep(0.01)
        assert arr.key_colors()[30] == (0, 0, 255)     # a change still goes out at once
    finally:
        e.stop()


def test_vendor_channel_still_gets_its_keepalive(tmp_path):
    from openalloy.infra import protocol as P
    from openalloy.infra.drivers import open_driver
    from openalloy.infra.simulator import SimulatedKeyboard
    kb = SimulatedKeyboard()
    e = _engine(tmp_path, lambda: open_driver(True, kb))
    try:
        e.apply({"effect": "static", "params": {"color": "#ff0000"}})
        time.sleep(0.5)
        n = sum(1 for p in kb.log if p[1] == P.CMD_STREAM)
        time.sleep(1.0)
        frames = (sum(1 for p in kb.log if p[1] == P.CMD_STREAM) - n) / P.FRAME_CHUNKS
        assert frames >= 3          # ~4/s: the firmware would fall back to its profile
    finally:
        e.stop()


# --- L1 / L2: --lang edge cases ---------------------------------------------------------------

def test_lang_without_value_shows_the_real_usage(capsys):
    from openalloy.presentation.cli.main import main
    with pytest.raises(SystemExit):
        main(["--lang"])
    err = capsys.readouterr().err
    assert err.startswith("usage: openalloy") and "--lang" in err


def test_unknown_lang_is_reported(capsys):
    from openalloy.presentation.cli.main import main
    assert main(["--lang", "klingon", "language"]) == 0
    assert "unknown language 'klingon'" in capsys.readouterr().err
    os.environ.pop("OPENALLOY_LANG", None)
