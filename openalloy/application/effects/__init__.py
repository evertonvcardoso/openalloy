# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Effect factory, composition with the per-key overlay, and onboard baking."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from openalloy.domain.colors import RGB
from openalloy.domain.layout import Layout
from openalloy.i18n import N_, Message

from .base import Effect, Lighting, Param
from .library import EFFECTS, REACTION_PARAMS, REGISTRY, ReactionLayer

__all__ = ["Effect", "Lighting", "Param", "EFFECTS", "REGISTRY", "Renderer",
           "create_effect", "bake", "describe_effects", "describe_reaction"]


def create_effect(effect_id: str, layout: Layout, values: Optional[Dict[str, Any]] = None
                  ) -> Effect:
    cls = REGISTRY.get(effect_id)
    if cls is None:
        raise ValueError(Message(N_("unknown effect: {effect!r}. Available: {available}"),
            effect=effect_id, available=", ".join(REGISTRY)))
    return cls(layout, values)


class Renderer:
    """Effect + reactive layer + per-key colours = what the keyboard shows."""

    def __init__(self, lighting: Lighting, layout: Layout, live: bool = True):
        self.lighting = lighting
        self.layout = layout
        self.effect = create_effect(lighting.effect, layout, lighting.params)
        self.reaction = ReactionLayer(layout, lighting.reaction)
        self._live = live
        self._overlay = {i: c for i, c in lighting.keys.items() if layout.key(i)}

    @property
    def animated(self) -> bool:
        return self.effect.animated or (self._live and self.reaction.active)

    @property
    def wants_keys(self) -> bool:
        return self.effect.live_only and not self.effect.needs_audio or self.reaction.active

    @property
    def needs_audio(self) -> bool:
        return self.effect.needs_audio

    def set_sound(self, sound) -> None:
        self.effect.sound = sound

    def frame(self, t: float) -> List[RGB]:
        out = self.effect.frame(t)
        if self._live:
            out = self.reaction.apply(out, t)
        for i, c in self._overlay.items():
            out[i] = c
        return out

    def on_key(self, index: int, t: float) -> None:
        self.effect.on_key(index, t)
        self.reaction.on_key(index, t)


def normalize(lighting: Lighting, layout: Layout) -> Lighting:
    """Validate and fill defaults; raises ValueError on bad input."""
    eff = create_effect(lighting.effect, layout, lighting.params)
    reaction = ReactionLayer(layout, lighting.reaction).json_values()
    return Lighting(lighting.effect, eff.json_values(), dict(lighting.keys), reaction)


def bake(lighting: Lighting, layout: Layout, frames: int) -> List[List[RGB]]:
    """Sample exactly one period into ``frames`` keyframes (seamless loop)."""
    r = Renderer(lighting, layout, live=False)   # no reactive layer in flash
    if not r.animated:
        frames = 1
    frames = max(1, min(255, int(frames)))
    period = r.effect.period()
    return [r.frame(period * n / frames) for n in range(frames)]


def describe_effects() -> List[dict]:
    out = []
    for cls in EFFECTS:
        out.append({
            "id": cls.id, "label": cls.label, "description": cls.description,
            "live_only": cls.live_only, "animated": cls.animated,
            "audio": cls.needs_audio,
            "params": [{
                "name": p.name, "label": p.label, "kind": p.kind, "default": p.default,
                "min": p.min, "max": p.max, "step": p.step, "choices": p.choices,
                "help": p.help,
            } for p in cls.params],
        })
    return out


def describe_reaction() -> List[dict]:
    return [{"name": p.name, "label": p.label, "kind": p.kind, "default": p.default,
             "min": p.min, "max": p.max, "step": p.step, "choices": p.choices,
             "help": p.help} for p in REACTION_PARAMS]
