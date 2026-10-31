# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Colour helpers. Colours are (r, g, b) tuples of ints 0-255."""

from __future__ import annotations

import colorsys
from typing import List, Sequence, Tuple

from openalloy.i18n import N_, Message

RGB = Tuple[int, int, int]
BLACK: RGB = (0, 0, 0)

NAMED = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0),
    "green": (0, 255, 0), "blue": (0, 0, 255), "yellow": (255, 220, 0),
    "cyan": (0, 255, 255), "magenta": (255, 0, 255), "orange": (255, 100, 0),
    "purple": (140, 0, 255), "pink": (255, 60, 150), "hyperx": (227, 25, 55),
    # Portuguese names (accepted as input)
    "preto": (0, 0, 0), "branco": (255, 255, 255), "vermelho": (255, 0, 0),
    "verde": (0, 255, 0), "azul": (0, 0, 255), "amarelo": (255, 220, 0),
    "ciano": (0, 255, 255), "laranja": (255, 100, 0), "roxo": (140, 0, 255),
    "rosa": (255, 60, 150),
}


def clamp8(v: float) -> int:
    v = int(round(v))
    return 0 if v < 0 else 255 if v > 255 else v


def parse_color(value) -> RGB:
    """Accept '#rrggbb', 'rrggbb', '#rgb', a colour name, or [r, g, b]."""
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return (clamp8(value[0]), clamp8(value[1]), clamp8(value[2]))
    s = str(value).strip().lower()
    if s in NAMED:
        return NAMED[s]
    if s.startswith("0x"):
        s = s[2:]
    s = s.lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise ValueError(Message(N_("invalid color: {value!r}"), value=value))
    try:
        n = int(s, 16)
    except ValueError as exc:
        raise ValueError(Message(N_("invalid color: {value!r}"), value=value)) from exc
    return ((n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF)


def to_hex(c: Sequence[int]) -> str:
    return "#%02x%02x%02x" % (clamp8(c[0]), clamp8(c[1]), clamp8(c[2]))


def scale(c: Sequence[float], k: float) -> RGB:
    return (clamp8(c[0] * k), clamp8(c[1] * k), clamp8(c[2] * k))


def lerp(a: Sequence[float], b: Sequence[float], t: float) -> RGB:
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    return (clamp8(a[0] + (b[0] - a[0]) * t),
            clamp8(a[1] + (b[1] - a[1]) * t),
            clamp8(a[2] + (b[2] - a[2]) * t))


def hsv(h: float, s: float = 1.0, v: float = 1.0) -> RGB:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return (clamp8(r * 255), clamp8(g * 255), clamp8(b * 255))


def gradient(stops: Sequence[Sequence[int]], t: float, cyclic: bool = False) -> RGB:
    """Sample a multi-stop gradient at t in [0, 1]."""
    stops = list(stops) or [BLACK]
    if len(stops) == 1:
        return tuple(stops[0])  # type: ignore[return-value]
    if cyclic:
        stops = stops + [stops[0]]
        t %= 1.0
    else:
        t = 0.0 if t < 0 else 1.0 if t > 1 else t
    pos = t * (len(stops) - 1)
    i = min(int(pos), len(stops) - 2)
    return lerp(stops[i], stops[i + 1], pos - i)


def add(a: Sequence[int], b: Sequence[int]) -> RGB:
    return (clamp8(a[0] + b[0]), clamp8(a[1] + b[1]), clamp8(a[2] + b[2]))


def max_blend(a: Sequence[int], b: Sequence[int]) -> RGB:
    return (max(a[0], b[0]), max(a[1], b[1]), max(a[2], b[2]))


# --- LED colour correction ----------------------------------------------------------
# Screen colours are gamma-encoded (sRGB); LEDs are driven linearly (PWM). Sent raw, a
# "screen red" such as #e01b24 lights green and blue at ~10% duty, which on a LED reads
# as pink. Converting to linear light makes the keyboard match what the screen shows.
DEFAULT_LED_GAMMA = 2.2
_LUTS: dict = {}


def _lut(gamma: float) -> List[int]:
    key = round(float(gamma), 2)
    lut = _LUTS.get(key)
    if lut is None:
        lut = [clamp8(255.0 * (i / 255.0) ** key) for i in range(256)]
        _LUTS[key] = lut
    return lut


def led_correct(colors: Sequence[RGB], gamma: float = DEFAULT_LED_GAMMA) -> List[RGB]:
    """Map screen colours to LED drive levels. gamma <= 1 leaves them untouched."""
    try:
        gamma = float(gamma)
    except (TypeError, ValueError):
        gamma = DEFAULT_LED_GAMMA
    if gamma != gamma:                     # NaN (e.g. a hand-edited config)
        gamma = DEFAULT_LED_GAMMA
    if gamma <= 1.0:
        return list(colors)
    lut = _lut(min(gamma, 3.0))
    return [(lut[c[0]], lut[c[1]], lut[c[2]]) for c in colors]
