# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Features adapted from hyperx-studio: reactive layer, sound effects, fire/snake,
frame de-duplication and system sleep handling."""

import json
import math
import subprocess
import sys
import time
from pathlib import Path

import pytest

from openalloy.application.effects import Lighting, Renderer, bake, normalize
from openalloy.application.engine import Engine
from openalloy.config.settings import PresetStore, Settings
from openalloy.domain.layout import get_layout
from openalloy.domain.sound import Sound
from openalloy.infra.audio import BLOCK, SAMPLE_RATE, Analyzer
from openalloy.infra.drivers import open_driver
from openalloy.infra.simulator import SimulatedKeyboard
from openalloy.presentation.cli.main import build_parser

LAYOUT = get_layout("abnt2")


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# --- reactive layer -------------------------------------------------------------

def test_reaction_flash_over_any_effect():
    light = Lighting("static", {"color": "#000000"}, {},
                     {"type": "flash", "color": "#ffffff", "fade": 1.0})
    r = Renderer(normalize(light, LAYOUT), LAYOUT)
    assert r.frame(0.0)[19] == (0, 0, 0)
    r.on_key(19, 0.0)
    assert r.frame(0.0)[19] == (255, 255, 255)
    assert 0 < r.frame(0.5)[19][0] < 255
    assert r.frame(1.5)[19] == (0, 0, 0)
    assert r.animated and r.wants_keys


def test_reaction_ripple_spreads_additively():
    light = normalize(Lighting("static", {"color": "#100000"}, {},
                               {"type": "ripple", "color": "#00ff00", "velocity": 10,
                                "fade": 2.0}), LAYOUT)
    r = Renderer(light, LAYOUT)
    r.on_key(LAYOUT.by_name("g").index, 0.0)
    frame = r.frame(0.2)          # ring radius 2 keys
    lit = [k.index for k in LAYOUT.keys if frame[k.index][1] > 0]
    assert lit and all(frame[i][0] >= 0x10 for i in lit)   # added on top of base


def test_per_key_colours_win_over_reaction():
    light = normalize(Lighting("static", {"color": "#000000"}, {19: (1, 2, 3)},
                               {"type": "flash"}), LAYOUT)
    r = Renderer(light, LAYOUT)
    r.on_key(19, 0.0)
    assert r.frame(0.0)[19] == (1, 2, 3)


def test_reaction_never_baked_into_flash():
    light = Lighting("static", {"color": "#202020"}, {}, {"type": "ripple"})
    assert bake(light, LAYOUT, 60) == [[c for c in Renderer(light, LAYOUT, live=False).frame(0)]]


def test_reaction_roundtrips_and_validates():
    d = normalize(Lighting.from_dict({"effect": "wave", "reaction": {"type": "flash"}}),
                  LAYOUT).to_dict()
    assert d["reaction"]["type"] == "flash" and d["reaction"]["color"] == "#ffffff"
    with pytest.raises(ValueError):
        normalize(Lighting("wave", {}, {}, {"type": "explode"}), LAYOUT)


# --- fire / snake ------------------------------------------------------------------

def test_fire_is_hotter_at_the_bottom():
    from openalloy.application.effects import create_effect
    f = create_effect("fire", LAYOUT, {"height": 100})
    frames = [f.frame(t) for t in (0, 0.3, 0.7, 1.1)]
    top = sum(sum(fr[LAYOUT.by_name("f5").index]) for fr in frames)
    bottom = sum(sum(fr[LAYOUT.by_name("space").index]) for fr in frames)
    assert bottom > top


def test_snake_has_exact_length():
    from openalloy.application.effects import create_effect
    s = create_effect("snake", LAYOUT, {"color": "#ffffff", "background": "#000000", "length": 10})
    lit = [c for c in s.frame(1.0) if c != (0, 0, 0)]
    assert 9 <= len(lit) <= 10


# --- sound -------------------------------------------------------------------------

def tone(hz, amp=0.5, n=BLOCK, offset=0):
    return [amp * math.sin(2 * math.pi * hz * (i + offset) / SAMPLE_RATE) for i in range(n)]


def test_analyzer_finds_the_right_band():
    a = Analyzer()
    for k in range(20):
        s = a.process(tone(1000, offset=k * BLOCK))
    assert max(range(12), key=lambda i: s.bands[i]) == 6     # 1 kHz band
    assert s.level > 0.5 and s.live


def test_analyzer_detects_beats():
    a = Analyzer()
    beats = []
    for k in range(60):
        kick = (k % 10) == 0
        block = [x + (0.8 * y if kick else 0) for x, y in
                 zip(tone(2500, 0.1, offset=k * BLOCK), tone(60, offset=k * BLOCK))]
        beats.append(a.process(block).beat)
    assert max(beats[20:]) > 0.6 and min(beats[20:]) < 0.2


def test_silence_is_not_live():
    a = Analyzer()
    for _ in range(5):
        s = a.process([0.0] * BLOCK)
    assert not s.live


@pytest.mark.parametrize("eid", ["equalizer", "soundspectrum", "soundpulse", "soundwave"])
def test_sound_effects_idle_and_react(eid):
    r = Renderer(normalize(Lighting(eid), LAYOUT), LAYOUT)
    assert r.needs_audio and not r.wants_keys
    idle = r.frame(0.5)
    assert any(c != (0, 0, 0) for c in idle)               # moving idle, never black
    r.set_sound(Sound(True, 1.0, 1.0, [1.0] * 12))
    loud = r.frame(0.55)
    assert sum(map(sum, loud)) > sum(map(sum, idle))


def test_equalizer_bars_follow_bands():
    r = Renderer(normalize(Lighting("equalizer", {"background": "#000000"}), LAYOUT), LAYOUT)
    r.set_sound(Sound(True, 0.5, 0.0, [1.0] * 6 + [0.0] * 6))   # only bass side
    f = r.frame(0)
    assert f[LAYOUT.by_name("leftctrl").index] != (0, 0, 0)
    assert f[LAYOUT.by_name("right").index] == (0, 0, 0)


# --- engine: dedupe, sleep, audio wiring ------------------------------------------

@pytest.fixture
def engine():
    kb = SimulatedKeyboard()
    e = Engine(lambda: open_driver(True, kb), Settings(), PresetStore(), enable_input=False)
    e.kb = kb
    e.start()
    assert wait_for(lambda: e.status()["connected"])
    yield e
    e.stop()


def test_static_frames_are_not_resent_every_tick(engine):
    engine.apply({"effect": "static", "params": {"color": "#123456"}})
    time.sleep(0.3)
    n = engine.status()["frames_sent"]
    time.sleep(1.0)
    sent = engine.status()["frames_sent"] - n
    assert 2 <= sent <= 6            # ~4/s (hold refresh) instead of 20/s


def test_suspend_releases_and_wake_restores(engine):
    engine.suspend()
    st = engine.status()
    assert st["asleep"] and not st["connected"]
    with pytest.raises(Exception):
        engine.set_brightness(10)
    engine.wake()
    assert wait_for(lambda: engine.status()["connected"])
    n = engine.status()["frames_sent"]
    assert wait_for(lambda: engine.status()["frames_sent"] > n)


def test_sound_frame_reports_inactive(engine):
    engine.apply({"effect": "wave"})
    assert engine.sound_frame()["on"] is False


# --- presets / cli / hook ----------------------------------------------------------

def test_new_presets_load():
    ids = {p["id"] for p in PresetStore().list()}
    assert {"cyberpunk", "lava", "equalizador", "maquina-de-escrever"} <= ids
    cp = PresetStore().get("cyberpunk")
    assert normalize(Lighting.from_dict(cp["lighting"]), LAYOUT).reaction["type"] == "ripple"


def test_cli_react_flags():
    a = build_parser().parse_args(["apply", "colorwave", "-r", "ripple", "--react-color", "rainbow"])
    assert a.react == "ripple" and a.react_color == "rainbow"


def test_sleep_hook_is_safe_without_daemons(tmp_path):
    hook = Path(__file__).resolve().parent.parent / "openalloy" / "resources" / "openalloy-sleep"
    for phase in ("pre", "post", "bogus"):
        r = subprocess.run([sys.executable, "-I", str(hook), phase, "suspend"], timeout=10)
        assert r.returncode == 0
    assert json  # keep import used
