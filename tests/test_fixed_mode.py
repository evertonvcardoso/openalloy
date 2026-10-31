# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""'Fixed' mode: colour applied once and kept by the keyboard, nothing streaming."""
import time

import pytest

from openalloy.application.engine import Engine
from openalloy.config.settings import PresetStore, Settings
from openalloy.infra.drivers import open_driver
from openalloy.infra.simulator import SimulatedKeyboard, SimulatedLampArray


def wait_for(cond, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def hp():
    arr = SimulatedLampArray()
    present = {"sig": ("/dev/hidraw4",)}
    e = Engine(lambda: open_driver("hp", arr), Settings(), PresetStore(), enable_input=False,
               presence=lambda: present["sig"])
    e.arr, e.present = arr, present
    e.start()
    assert wait_for(lambda: e.status()["connected"])
    yield e
    e.stop()


def test_fix_applies_once_then_stops_streaming(hp):
    hp.fix({"effect": "static", "params": {"color": "#ff0000"}, "keys": {"19": "#ffffff"}})
    assert wait_for(lambda: hp.status()["fixed_applied"])
    assert hp.arr.key_colors()[19] == (255, 255, 255) and hp.arr.key_colors()[26] == (255, 0, 0)
    assert hp.arr.autonomous is False
    n = len(hp.arr.log)
    time.sleep(0.6)
    assert len(hp.arr.log) == n                     # nothing sent while fixed
    assert hp.status()["connected"] is True
    assert hp.settings.get("mode") == "fixed"


def test_fix_survives_engine_stop(hp):
    hp.fix({"effect": "static", "params": {"color": "#00ff00"}})
    assert wait_for(lambda: hp.status()["fixed_applied"])
    hp.stop()
    assert hp.arr.autonomous is False               # LEDs were not handed back


def test_replug_reapplies(hp):
    hp.fix({"effect": "static", "params": {"color": "#0000ff"}})
    assert wait_for(lambda: hp.status()["fixed_applied"])
    hp.arr.colors = {k: ((0, 0, 0), 0) for k in hp.arr.colors}   # keyboard reset
    hp.arr.autonomous = True
    hp.present["sig"] = ("/dev/hidraw7",)                         # new node after replug
    hp._wake.set()
    assert wait_for(lambda: hp.arr.autonomous is False, 6)
    assert hp.arr.key_colors()[30] == (0, 0, 255)


def test_brightness_change_reapplies(hp):
    hp.fix({"effect": "static", "params": {"color": "#c80000"}})
    assert wait_for(lambda: hp.status()["fixed_applied"])
    hp.set_brightness(128)
    from openalloy.domain.colors import led_correct
    red = round(led_correct([(200, 0, 0)])[0][0] * 128 / 255)
    assert wait_for(lambda: hp.arr.key_colors()[30] == (red, 0, 0), 5)


def test_live_after_fix_streams_again(hp):
    hp.fix({"effect": "static", "params": {"color": "#ff0000"}})
    assert wait_for(lambda: hp.status()["fixed_applied"])
    hp.apply({"effect": "spectrum"})
    n = len(hp.arr.log)
    assert wait_for(lambda: len(hp.arr.log) > n + 5)


def test_vendor_channel_falls_back_to_streaming():
    kb = SimulatedKeyboard()
    e = Engine(lambda: open_driver(True, kb), Settings(), PresetStore(), enable_input=False,
               presence=lambda: ())
    e.start()
    try:
        e.fix({"effect": "static", "params": {"color": "#123456"}})
        n = e.status()["frames_sent"]
        assert wait_for(lambda: e.status()["frames_sent"] > n + 2)
    finally:
        e.stop()
