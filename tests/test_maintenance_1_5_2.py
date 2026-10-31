# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Maintenance items from the external 1.5.1 review (section 4)."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from openalloy.config.settings import PresetStore, Settings

ROOT = Path(__file__).resolve().parent.parent


def wait_until(cond, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


# --- settings and presets given from outside --------------------------------------------------

def test_oversized_config_file_is_ignored(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"fps": 20, "junk": "x" * (2 * 1024 * 1024)}))
    st = Settings(cfg)
    assert st.get("fps") == 30                       # defaults, file not loaded


def test_deeply_nested_config_does_not_crash(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text("[" * 100_000 + "]" * 100_000)   # RecursionError territory
    assert Settings(cfg).get("fps") == 30


def test_malformed_validation_records_are_dropped(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"flash_validated": {"a": "not-a-record", "b": {"firmware": "1"}},
                               "lighting": {"effect": "static", "keys": {str(i): "#fff"
                                                                       for i in range(5000)}}}))
    st = Settings(cfg)
    assert list(st.get("flash_validated")) == ["b"]
    assert len(st.get("lighting")["keys"]) <= 94


def test_oversized_preset_file_is_skipped(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "huge.json").write_text(json.dumps({"name": "Huge", "lighting": {
        "effect": "static", "params": {"color": "#fff"}, "pad": "x" * (600 * 1024)}}))
    (d / "ok.json").write_text('{"name": "Ok", "lighting": {"effect": "static"}}')
    assert [p["id"] for p in PresetStore(d, tmp_path / "none").list()] == ["ok"]


def test_names_that_differ_only_in_case_or_punctuation_are_the_same_preset(tmp_path):
    ps = PresetStore(tmp_path / "p", tmp_path / "none")
    a = ps.save("My Preset", {"effect": "static"})
    b = ps.save("my-preset!", {"effect": "wave"})
    assert a == b == "my-preset"
    assert [p["lighting"]["effect"] for p in ps.list()] == ["wave"]     # the last save wins


def test_concurrent_delete_is_safe(tmp_path):
    ps = PresetStore(tmp_path / "p", tmp_path / "none")
    ps.save("Gone", {"effect": "static"})
    results, errors = [], []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            results.append(ps.delete("gone"))
        except Exception as exc:          # noqa: BLE001 - the test reports any error
            errors.append(exc)
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and results.count(True) == 1


# --- engine ---------------------------------------------------------------------------------

@pytest.fixture
def hp(tmp_path):
    from openalloy.application.engine import Engine
    from openalloy.infra.drivers import open_driver
    from openalloy.infra.simulator import SimulatedLampArray
    arr = SimulatedLampArray()
    e = Engine(lambda: open_driver("hp", arr), Settings(tmp_path / "c.json"),
               PresetStore(tmp_path / "p"), enable_input=False)
    e.arr = arr
    e.start()
    assert wait_until(lambda: e.status()["connected"])
    yield e
    e.stop()


def test_brightness_right_after_an_effect_change(hp):
    for _attempt in range(5):
        hp.apply({"effect": "static", "params": {"color": "#ffffff"}})
        hp.set_brightness(64)
        assert wait_until(lambda: hp.arr.key_colors()[30] == (64, 64, 64), 1.5)
        hp.apply({"effect": "static", "params": {"color": "#ff0000"}})
        hp.set_brightness(255)
        assert wait_until(lambda: hp.arr.key_colors()[30] == (255, 0, 0), 1.5)


def test_key_reader_comes_back_after_suspend(tmp_path, monkeypatch):
    from openalloy.application.engine import Engine
    from openalloy.infra import input as I
    from openalloy.infra.drivers import open_driver
    from openalloy.infra.simulator import SimulatedLampArray
    opened = []
    fds = []

    def fake_open(path, flags, *a):
        if path == "/dev/fake-kbd":
            r, w = os.pipe()
            fds.append(w)
            opened.append(time.time())
            return r
        return real_open(path, flags, *a)
    real_open = os.open
    monkeypatch.setattr(I, "find_event_nodes", lambda ids: ["/dev/fake-kbd"])
    monkeypatch.setattr(os, "open", fake_open)
    arr = SimulatedLampArray()
    e = Engine(lambda: open_driver("hp", arr), Settings(tmp_path / "c.json"),
               PresetStore(tmp_path / "p"), enable_input=True)
    e.start()
    try:
        e.apply({"effect": "reactive"})
        assert wait_until(lambda: len(opened) == 1)
        e.suspend()
        assert wait_until(lambda: e.status()["input"] == "stopped")
        e.wake()
        assert wait_until(lambda: len(opened) == 2, 6)       # node opened again after wake
        assert wait_until(lambda: e.status()["input"].startswith("active"), 3)
    finally:
        e.stop()
        if e._input:
            e._input._thread.join(2)
        for w in fds:
            os.close(w)


# --- the service process --------------------------------------------------------------------

def test_service_stops_quickly_with_a_client_connected(tmp_path):
    env = dict(os.environ, XDG_RUNTIME_DIR=str(tmp_path / "run"), PYTHONPATH=str(ROOT),
               XDG_CONFIG_HOME=str(tmp_path / "cfg"))
    (tmp_path / "run").mkdir(mode=0o700, exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-m", "openalloy.presentation.daemon.main",
                             "--simulate-hp"], env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    sock = tmp_path / "run" / "openalloy.sock"
    try:
        from openalloy.infra.rpc import RpcClient, is_alive
        assert wait_until(lambda: is_alive(sock), 10)
        stop = threading.Event()
        sub = RpcClient(sock).subscribe(lambda k, d: None, stop)   # a window listening
        time.sleep(0.5)
        t0 = time.time()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=8)
        assert time.time() - t0 < 5
        assert proc.returncode == 0
        assert not sock.exists()
        stop.set()
        sub.join(3)
        assert not sub.is_alive()
    finally:
        if proc.poll() is None:
            proc.kill()


def test_cli_starts_with_a_corrupted_config(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config" / "openalloy"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.json").write_text("[" * 100_000 + "]" * 100_000)
    from openalloy import i18n
    assert i18n.resolve() == "en"
    from openalloy.presentation.cli.main import main
    assert main(["language"]) == 0
    assert "English" in capsys.readouterr().out
