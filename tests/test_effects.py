# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
import pytest

from openalloy.application.effects import (
    EFFECTS,
    REGISTRY,
    Lighting,
    Renderer,
    bake,
    create_effect,
    normalize,
)
from openalloy.domain.layout import get_layout

LAYOUT = get_layout("abnt2")


@pytest.mark.parametrize("cls", EFFECTS, ids=lambda c: c.id)
def test_every_effect_renders_valid_frames(cls):
    eff = cls(LAYOUT, {})
    for t in (0.0, 0.37, 5.0):
        frame = eff.frame(t)
        assert len(frame) == 94
        for c in frame:
            assert len(c) == 3 and all(isinstance(v, int) and 0 <= v <= 255 for v in c)


@pytest.mark.parametrize("cls", [c for c in EFFECTS if not c.live_only], ids=lambda c: c.id)
def test_effects_loop_seamlessly(cls):
    eff = cls(LAYOUT, {})
    p = eff.period()
    a, b = eff.frame(0.0), eff.frame(p)
    diff = max(abs(x - y) for ca, cb in zip(a, b) for x, y in zip(ca, cb))
    assert diff <= 2, f"{cls.id} is not periodic over {p}s"


def test_effects_are_deterministic():
    for cls in EFFECTS:
        if cls.live_only:
            continue
        assert cls(LAYOUT, {}).frame(1.23) == cls(LAYOUT, {}).frame(1.23)


def test_param_coercion_and_validation():
    e = create_effect("wave", LAYOUT, {"speed": "500", "direction": "left"})
    assert e.v("speed") == 100 and e.v("direction") == "left"
    with pytest.raises(ValueError):
        create_effect("wave", LAYOUT, {"direction": "diagonal"})
    with pytest.raises(ValueError):
        create_effect("nope", LAYOUT, {})
    e = create_effect("colorwave", LAYOUT, {"colors": "red, #00ff00 ,blue"})
    assert e.v("colors") == [(255, 0, 0), (0, 255, 0), (0, 0, 255)]


def test_overlay_wins_over_effect():
    light = Lighting("static", {"color": "#000000"}, {19: (1, 2, 3)})
    assert Renderer(light, LAYOUT).frame(0)[19] == (1, 2, 3)


def test_bake_static_is_one_frame_and_animated_respects_count():
    assert len(bake(Lighting("static"), LAYOUT, 100)) == 1
    frames = bake(Lighting("wave"), LAYOUT, 40)
    assert len(frames) == 40 and frames[0] != frames[20]
    assert len(bake(Lighting("wave"), LAYOUT, 999)) == 255


def test_reactive_responds_to_keys():
    eff = REGISTRY["reactive"](LAYOUT, {"color": "#ffffff", "background": "#000000", "fade": 1})
    assert eff.frame(0.0)[19] == (0, 0, 0)
    eff.on_key(19, 0.0)
    assert eff.frame(0.0)[19] == (255, 255, 255)
    assert eff.frame(0.5)[19][0] < 255
    assert eff.frame(2.0)[19] == (0, 0, 0)


def test_normalize_fills_defaults():
    n = normalize(Lighting("scanner"), LAYOUT)
    assert set(n.params) == {"color", "background", "speed", "width", "vertical"}
    assert n.params["color"].startswith("#")
