# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Effect framework.

Linuity's effects were stateful objects stepping a 2-zone intensity on every
daemon tick. Here every effect is a pure, *cyclic* function of time:

    colors = effect.frame(t)        # 94 RGB tuples

Purity buys three things: the GUI preview is pixel-identical to what the
keyboard shows, the daemon can drop/skip ticks without drifting, and any
effect can be baked into N keyframes for the onboard memory (one period
sampled uniformly loops seamlessly on the firmware).

Reactive effects are the exception: they also consume key-press events and
only make sense in live mode; when baked they render their idle state.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from openalloy.domain.colors import RGB, parse_color
from openalloy.domain.layout import NUM_LEDS, Layout
from openalloy.domain.sound import Sound
from openalloy.i18n import N_, Message

# Parameter kinds understood by the GUI and CLI.
COLOR = "color"
COLORS = "colors"
INT = "int"
FLOAT = "float"
CHOICE = "choice"
BOOL = "bool"

DIRECTIONS = {
    "right": N_("Left → right"),
    "left": N_("Right → left"),
    "down": N_("Top → bottom"),
    "up": N_("Bottom → top"),
    "out": N_("Center → edges"),
    "in": N_("Edges → center"),
}


@dataclass
class Param:
    name: str
    label: str
    kind: str
    default: Any
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    choices: Optional[Dict[str, str]] = None
    help: str = ""

    def coerce(self, value: Any) -> Any:
        """Validate one value; always raises ValueError (never TypeError etc.)."""
        try:
            return self._coerce(value)
        except ValueError:
            raise
        except (TypeError, OverflowError, AttributeError) as exc:
            raise ValueError(Message(N_("{name}: invalid value {value!r}"),
                name=self.name, value=value)) from exc

    def _coerce(self, value: Any) -> Any:
        if value is None:
            value = self.default
        if self.kind == COLOR:
            return parse_color(value)
        if self.kind == COLORS:
            if isinstance(value, str):
                value = [v for v in value.replace(";", ",").split(",") if v.strip()]
            cols = [parse_color(v) for v in value][:6]
            return cols or [parse_color(c) for c in self.default]
        if self.kind in (INT, FLOAT):
            v = self._number(value)
            if self.kind == INT:
                v = int(round(v))
                return int(max(self.min, min(self.max, v))) if self.min is not None else v
            return max(self.min, min(self.max, v)) if self.min is not None else v
        if self.kind == BOOL:
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "sim", "on")
            return bool(value)
        if self.kind == CHOICE:
            v = str(value)
            if self.choices and v not in self.choices:
                raise ValueError(Message(N_("{name}: invalid option {value!r}; use {choices}"),
                    name=self.name, value=v, choices=", ".join(self.choices)))
            return v
        return value

    def _number(self, value: Any) -> float:
        """float(value) with our own, translatable message ("abc", "nan" and "inf" gave
        Python's untranslated text or slipped through as "invalid value")."""
        if isinstance(value, bool):
            raise ValueError(Message(N_("{name}: a number is expected"), name=self.name))
        try:
            v = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(Message(N_("{name}: a number is expected"), name=self.name)) from None
        if not math.isfinite(v):
            raise ValueError(Message(N_("{name}: a number is expected"), name=self.name))
        return v

    def to_json(self, value: Any) -> Any:
        from openalloy.domain.colors import to_hex
        if self.kind == COLOR:
            return to_hex(value)
        if self.kind == COLORS:
            return [to_hex(c) for c in value]
        return value


def speed_param(default: int = 30) -> Param:
    return Param("speed", N_("Speed"), INT, default, 1, 100, 1,
                 help=N_("1 = a ~10 s cycle, 100 = a ~0.6 s cycle"))


def period_from_speed(speed: float) -> float:
    return 60.0 / (float(speed) + 5.0)


def key_hash(*parts) -> float:
    """Deterministic pseudo-random number in [0, 1)."""
    h = hashlib.blake2b(repr(parts).encode(), digest_size=4).digest()
    return int.from_bytes(h, "little") / 2 ** 32


class Effect:
    id: str = ""
    label: str = ""
    description: str = ""
    params: List[Param] = []
    live_only: bool = False      # needs runtime input (keys or sound)
    animated: bool = True
    needs_audio: bool = False    # engine feeds ``self.sound`` every frame

    def __init__(self, layout: Layout, values: Optional[Dict[str, Any]] = None):
        self.layout = layout
        self.sound = Sound()
        self.values: Dict[str, Any] = {}
        values = values or {}
        for p in self.params:
            self.values[p.name] = p.coerce(values.get(p.name))
        self.setup()

    # hooks ---------------------------------------------------------------
    def setup(self) -> None:
        pass

    def period(self) -> float:
        """Seconds for one full, seamlessly looping cycle."""
        if "speed" in self.values:
            return period_from_speed(self.values["speed"])
        return 1.0

    def color_for(self, key, t: float, phase: float) -> RGB:
        """Per-key colour; ``phase`` is t / period in [0, 1)."""
        raise NotImplementedError

    def frame(self, t: float) -> List[RGB]:
        out: List[RGB] = [(0, 0, 0)] * NUM_LEDS
        period = self.period() or 1.0
        phase = (t / period) % 1.0
        for key in self.layout.keys:
            out[key.index] = self.color_for(key, t, phase)
        return out

    def on_key(self, index: int, t: float) -> None:
        pass

    # helpers -------------------------------------------------------------
    def v(self, name: str) -> Any:
        return self.values[name]

    def directional(self, key, direction: str) -> float:
        """Position of a key along a direction, 0..1."""
        if direction == "right":
            return key.u
        if direction == "left":
            return 1.0 - key.u
        if direction == "down":
            return key.v
        if direction == "up":
            return 1.0 - key.v
        d = math.hypot((key.u - 0.5) * 1.0, (key.v - 0.5) * 0.35) / 0.52
        d = min(1.0, d)
        return d if direction == "out" else 1.0 - d

    def json_values(self) -> Dict[str, Any]:
        return {p.name: p.to_json(self.values[p.name]) for p in self.params}


@dataclass
class Lighting:
    """A complete lighting setup: effect + parameters + per-key overlay."""
    effect: str = "static"
    params: Dict[str, Any] = field(default_factory=dict)
    keys: Dict[int, RGB] = field(default_factory=dict)   # overlay, wins over effect
    reaction: Dict[str, Any] = field(default_factory=dict)  # key-press layer (live)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Lighting:
        data = data if isinstance(data, dict) else {}
        keys = {}
        raw_keys = data.get("keys")
        for k, v in (raw_keys.items() if isinstance(raw_keys, dict) else ()):
            try:
                keys[int(k)] = parse_color(v)
            except (TypeError, ValueError):
                continue
        params, reaction = data.get("params"), data.get("reaction")
        return cls(effect=str(data.get("effect", "static")),
                   params=dict(params) if isinstance(params, dict) else {}, keys=keys,
                   reaction=dict(reaction) if isinstance(reaction, dict) else {})

    def to_dict(self) -> dict:
        from openalloy.domain.colors import to_hex
        return {"effect": self.effect, "params": dict(self.params),
                "keys": {str(k): to_hex(v) for k, v in sorted(self.keys.items())},
                "reaction": dict(self.reaction)}


Frame = Sequence[Tuple[int, int, int]]
