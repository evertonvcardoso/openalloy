# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""LED colour correction: screen reds must not turn pink on the keyboard."""
import time

from openalloy.application.backend import make_handlers
from openalloy.application.engine import Engine
from openalloy.config.settings import PresetStore, Settings
from openalloy.domain.colors import led_correct
from openalloy.infra.drivers import open_driver
from openalloy.infra.simulator import SimulatedLampArray


def test_pure_colours_are_unchanged():
    assert led_correct([(255, 0, 0), (0, 0, 0), (255, 255, 255)]) == \
        [(255, 0, 0), (0, 0, 0), (255, 255, 255)]


def test_gtk_palette_red_becomes_red():
    for screen_red in ((224, 27, 36), (237, 51, 59), (192, 28, 40)):
        r, g, b = led_correct([screen_red])[0]
        assert g <= 10 and b <= 12 and r >= 130


def test_gamma_one_disables():
    assert led_correct([(224, 27, 36)], 1.0) == [(224, 27, 36)]


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def test_engine_sends_corrected_colours(tmp_path):
    arr = SimulatedLampArray()
    st = Settings(tmp_path / "c.json")
    e = Engine(lambda: open_driver("hp", arr), st, PresetStore(tmp_path / "p"),
               enable_input=False)
    e.start()
    try:
        e.apply({"effect": "static", "params": {"color": "#e01b24"}})

        def corrected():
            r, g, b = arr.key_colors()[19]
            return g <= 10 and b <= 12 and r > 150
        assert wait_for(corrected)                 # polls: no fixed sleep (load-tolerant)
        make_handlers(e)["settings_update"](led_gamma=1.0)
        assert wait_for(lambda: arr.key_colors()[19] == (224, 27, 36))
    finally:
        e.stop()
