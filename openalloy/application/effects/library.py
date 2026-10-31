# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Built-in effects.

Ported from Linuity (generalised from 2-zone intensity to per-key RGB):
blink, gradual -> breathing, flicker, scanner, wave.
New for the keyboard: static, spectrum, rainbow wave, colour wave, gradient,
starlight, rain, reactive and ripple (live only).
Ideas adapted from hyperx-studio (MIT): fire, snake, the four sound-reactive
effects and the reactive layer that can sit on top of any effect. Fire and
snake were rewritten to loop seamlessly so they can be baked into onboard
memory.
"""

from __future__ import annotations

import math
import threading
from typing import Dict, List

from openalloy.domain import colors as C
from openalloy.domain.layout import NUM_LEDS
from openalloy.i18n import N_

from .base import (
    BOOL,
    CHOICE,
    COLOR,
    COLORS,
    DIRECTIONS,
    FLOAT,
    INT,
    Effect,
    Param,
    key_hash,
    period_from_speed,
    speed_param,
)

TAU = 2 * math.pi


class Off(Effect):
    id, label = "off", N_("Off")
    description = N_("Every key dark.")
    params: List[Param] = []
    animated = False

    def color_for(self, key, t, phase):
        return (0, 0, 0)


class Static(Effect):
    id, label = "static", N_("Static color")
    description = N_("One color on the whole keyboard. Use per-key painting to highlight keys.")
    params = [Param("color", N_("Color"), COLOR, "#e31937")]
    animated = False

    def color_for(self, key, t, phase):
        return self.values["color"]


class Breathing(Effect):
    id, label = "breathing", N_("Breathing")
    description = N_("Fades in and out smoothly, cycling through the colors (Linuity's gradual).")
    params = [
        Param("colors", N_("Colors"), COLORS, ["#e31937", "#2a7fff"]),
        speed_param(25),
        Param("min", N_("Minimum brightness (%)"), INT, 0, 0, 100, 1),
    ]

    def period(self):
        return period_from_speed(self.v("speed")) * 2 * len(self.v("colors"))

    def color_for(self, key, t, phase):
        cols = self.v("colors")
        n = len(cols)
        pos = phase * n
        idx = int(pos) % n
        k = math.sin(math.pi * (pos - int(pos)))
        lo = self.v("min") / 100.0
        return C.scale(cols[idx], lo + (1 - lo) * k * k)


class Spectrum(Effect):
    id, label = "spectrum", N_("Color cycle")
    description = N_("The whole keyboard sweeps through the spectrum.")
    params = [speed_param(15), Param("saturation", N_("Saturation (%)"), INT, 100, 0, 100, 1)]

    def period(self):
        return period_from_speed(self.v("speed")) * 4

    def color_for(self, key, t, phase):
        return C.hsv(phase, self.v("saturation") / 100.0)


class RainbowWave(Effect):
    id, label = "wave", N_("Rainbow wave")
    description = N_("A rainbow sliding across the keyboard.")
    params = [
        speed_param(30),
        Param("direction", N_("Direction"), CHOICE, "right", choices=DIRECTIONS),
        Param("scale", N_("Wave width"), INT, 100, 20, 400, 5, help=N_("% of the keyboard per color cycle")),
        Param("saturation", N_("Saturation (%)"), INT, 100, 0, 100, 1),
    ]

    def period(self):
        return period_from_speed(self.v("speed")) * 2

    def color_for(self, key, t, phase):
        pos = self.directional(key, self.v("direction")) * 100.0 / self.v("scale")
        return C.hsv(pos - phase, self.v("saturation") / 100.0)


class ColorWave(Effect):
    id, label = "colorwave", N_("Color wave")
    description = N_("A gradient of your colors rolling across the keyboard.")
    params = [
        Param("colors", N_("Colors"), COLORS, ["#e31937", "#ff8a00", "#7a00ff"]),
        speed_param(30),
        Param("direction", N_("Direction"), CHOICE, "right", choices=DIRECTIONS),
        Param("scale", N_("Wave width"), INT, 100, 20, 400, 5),
    ]

    def period(self):
        return period_from_speed(self.v("speed")) * 2

    def color_for(self, key, t, phase):
        pos = self.directional(key, self.v("direction")) * 100.0 / self.v("scale")
        return C.gradient(self.v("colors"), pos - phase, cyclic=True)


class Gradient(Effect):
    id, label = "gradient", N_("Static gradient")
    description = N_("A still gradient between the colors.")
    params = [
        Param("colors", N_("Colors"), COLORS, ["#00d4ff", "#ff00c8"]),
        Param("direction", N_("Direction"), CHOICE, "right", choices=DIRECTIONS),
    ]
    animated = False

    def color_for(self, key, t, phase):
        return C.gradient(self.v("colors"), self.directional(key, self.v("direction")))


class Scanner(Effect):
    id, label = "scanner", N_("Scanner")
    description = N_("A band sweeping back and forth with a trail (Linuity's scanner).")
    params = [
        Param("color", N_("Color"), COLOR, "#ff0000"),
        Param("background", N_("Background"), COLOR, "#100000"),
        speed_param(30),
        Param("width", N_("Width (%)"), INT, 18, 4, 60, 1),
        Param("vertical", N_("Vertical"), BOOL, False),
    ]

    def period(self):
        return period_from_speed(self.v("speed")) * 2

    def color_for(self, key, t, phase):
        center = (math.sin(TAU * phase - math.pi / 2) + 1) / 2
        pos = key.v if self.v("vertical") else key.u
        d = abs(pos - center) / (self.v("width") / 100.0)
        k = max(0.0, 1.0 - d) ** 3  # cubic contrast curve, as in Linuity
        return C.lerp(self.v("background"), self.v("color"), k)


class Blink(Effect):
    id, label = "blink", N_("Blink")
    description = N_("Turns on and off (Linuity's blink).")
    params = [
        Param("color", N_("Color"), COLOR, "#ffffff"),
        Param("interval", N_("Interval (s)"), FLOAT, 0.5, 0.05, 5.0, 0.05),
        Param("duty", N_("Time lit (%)"), INT, 50, 5, 95, 1),
    ]

    def period(self):
        return self.v("interval") * 2

    def color_for(self, key, t, phase):
        return self.v("color") if phase < self.v("duty") / 100.0 else (0, 0, 0)


class Flicker(Effect):
    id, label = "flicker", N_("Candle")
    description = N_("Candle-like flicker, independent per key (Linuity's flicker).")
    params = [
        Param("color", N_("Color"), COLOR, "#ff6a00"),
        Param("min", N_("Minimum brightness (%)"), INT, 35, 0, 100, 1),
        Param("max", N_("Maximum brightness (%)"), INT, 100, 0, 100, 1),
        speed_param(40),
    ]

    def period(self):
        return period_from_speed(self.v("speed")) * 8

    def color_for(self, key, t, phase):
        i = key.index
        # Sum of integer-harmonic sines -> random-looking yet perfectly looping.
        n = 0.0
        for h, w in ((3, 0.5), (7, 0.3), (13, 0.2)):
            n += w * math.sin(TAU * (h * phase + key_hash("fl", i, h)))
        k = (n + 1) / 2
        lo, hi = self.v("min") / 100.0, self.v("max") / 100.0
        return C.scale(self.v("color"), lo + (hi - lo) * k)


class Starlight(Effect):
    id, label = "starlight", N_("Starlight")
    description = N_("Keys light up and fade at random.")
    params = [
        Param("colors", N_("Star colors"), COLORS, ["#ffffff", "#7fd4ff", "#ffd27f"]),
        Param("background", N_("Background"), COLOR, "#02030a"),
        Param("density", N_("Density (%)"), INT, 35, 1, 100, 1),
        speed_param(20),
    ]

    def period(self):
        return period_from_speed(self.v("speed")) * 4

    def color_for(self, key, t, phase):
        i = key.index
        cols = self.v("colors")
        bg = self.v("background")
        best = bg
        # Two independent twinkle slots per key, so dense skies stay lively.
        for slot in range(2):
            if key_hash("st", i, slot) > self.v("density") / 100.0:
                continue
            p = (phase + key_hash("ph", i, slot)) % 1.0
            life = 0.35
            if p < life:
                k = math.sin(math.pi * p / life)
                c = cols[int(key_hash("c", i, slot) * len(cols)) % len(cols)]
                best = C.max_blend(best, C.lerp(bg, c, k))
        return best


class Rain(Effect):
    id, label = "rain", N_("Digital rain")
    description = N_("Drops falling down each column, with a trail.")
    params = [
        Param("color", N_("Color"), COLOR, "#00ff55"),
        Param("background", N_("Background"), COLOR, "#000800"),
        speed_param(30),
        Param("trail", N_("Trail (%)"), INT, 45, 5, 100, 1),
    ]

    def period(self):
        return period_from_speed(self.v("speed")) * 3

    def color_for(self, key, t, phase):
        col = int(key.cx * 2)  # half-unit columns
        p = (phase * 2 + key_hash("rain", col)) % 1.0
        head = p * 1.6 - 0.3          # travels from above to below the board
        d = head - key.v
        trail = self.v("trail") / 100.0
        k = 0.0
        if 0 <= d <= trail:
            k = (1 - d / trail) ** 2
        return C.lerp(self.v("background"), self.v("color"), k)


class _ReactiveBase(Effect):
    live_only = True

    def setup(self):
        self._hits: Dict[int, float] = {}
        self._lock = threading.Lock()

    def on_key(self, index, t):
        with self._lock:
            self._hits[index] = t

    def _snapshot(self, t, horizon):
        with self._lock:
            for i in [i for i, h in self._hits.items() if t - h > horizon]:
                del self._hits[i]
            return dict(self._hits)


class Reactive(_ReactiveBase):
    id, label = "reactive", N_("Reactive")
    description = N_("Each pressed key lights up and fades (live mode only).")
    params = [
        Param("color", N_("Color on key press"), COLOR, "#ffffff"),
        Param("background", N_("Background"), COLOR, "#140004"),
        Param("fade", N_("Duration (s)"), FLOAT, 0.8, 0.1, 5.0, 0.1),
    ]
    animated = True

    def frame(self, t):
        out = [(0, 0, 0)] * NUM_LEDS
        fade = self.v("fade")
        hits = self._snapshot(t, fade)
        bg, fg = self.v("background"), self.v("color")
        for key in self.layout.keys:
            h = hits.get(key.index)
            k = 0.0 if h is None else max(0.0, 1 - (t - h) / fade)
            out[key.index] = C.lerp(bg, fg, k)
        return out


class Ripple(_ReactiveBase):
    id, label = "ripple", N_("Ripple")
    description = N_("Waves spread out from the pressed key (live mode only).")
    params = [
        Param("color", N_("Wave color"), COLOR, "#00e5ff"),
        Param("background", N_("Background"), COLOR, "#000814"),
        Param("rainbow", N_("Rainbow"), BOOL, False),
        Param("velocity", N_("Speed (keys/s)"), FLOAT, 14.0, 2.0, 40.0, 1.0),
        Param("width", N_("Thickness (keys)"), FLOAT, 1.5, 0.5, 5.0, 0.5),
    ]

    def frame(self, t):
        out = [(0, 0, 0)] * NUM_LEDS
        vel, width = self.v("velocity"), self.v("width")
        horizon = 20.0 / vel
        hits = self._snapshot(t, horizon)
        origins = []
        for idx, h in hits.items():
            k = self.layout.key(idx)
            if k:
                origins.append((k.cx, k.cy, t - h))
        bg, fg = self.v("background"), self.v("color")
        for key in self.layout.keys:
            best = 0.0
            hue = 0.0
            for ox, oy, age in origins:
                r = vel * age
                d = abs(math.hypot(key.cx - ox, key.cy - oy) - r)
                if d < width:
                    k = (1 - d / width) * max(0.0, 1 - age / horizon)
                    if k > best:
                        best, hue = k, (r / 18.0)
            col = C.hsv(hue) if self.v("rainbow") else fg
            out[key.index] = C.lerp(bg, col, best)
        return out


class Fire(Effect):
    id, label = "fire", N_("Fire")
    description = N_("Flames rising from the bottom keys, flickering.")
    params = [speed_param(30), Param("height", N_("Flame height (%)"), INT, 60, 10, 100, 1)]

    _STOPS = ((60, 0, 0), (255, 40, 0), (255, 170, 0), (255, 245, 190))

    def period(self):
        return period_from_speed(self.v("speed")) * 4

    def color_for(self, key, t, phase):
        x = key.cx
        # integer harmonics of the phase -> loops exactly over one period
        n = (0.5 * math.sin(x * 1.7 + TAU * 3 * phase)
             + 0.3 * math.sin(x * 0.6 - TAU * 2 * phase)
             + 0.2 * math.sin(x * 3.3 + TAU * 5 * phase))
        h = self.v("height") / 100.0
        heat = (key.v - (1 - h)) / max(h, 0.05) + n * 0.35
        heat = max(0.0, min(1.0, heat))
        if heat < 0.45:
            return C.lerp(self._STOPS[0], self._STOPS[1], heat / 0.45)
        if heat < 0.8:
            return C.lerp(self._STOPS[1], self._STOPS[2], (heat - 0.45) / 0.35)
        return C.lerp(self._STOPS[2], self._STOPS[3], (heat - 0.8) / 0.2)


class Snake(Effect):
    id, label = "snake", N_("Snake")
    description = N_("A snake crawls across the keyboard row by row, zig-zagging.")
    params = [
        Param("color", N_("Color"), COLOR, "#fff000"),
        Param("background", N_("Background"), COLOR, "#060606"),
        speed_param(30),
        Param("length", N_("Length (keys)"), INT, 14, 2, 60, 1),
        Param("reverse", N_("Reverse direction"), BOOL, False),
    ]

    def setup(self):
        rows: Dict[int, list] = {}
        for k in self.layout.keys:
            rows.setdefault(k.row, []).append(k)
        order = []
        for r in sorted(rows):
            ks = sorted(rows[r], key=lambda k: k.x)
            order += ks if r % 2 == 0 else ks[::-1]
        self._pos = {k.index: n for n, k in enumerate(order)}
        self._n = max(1, len(order))

    def period(self):
        return self._n * 0.06 * 30 / self.v("speed")

    def color_for(self, key, t, phase):
        head = phase * self._n
        pos = self._pos[key.index]
        if self.v("reverse"):
            pos = self._n - 1 - pos
        d = (head - pos) % self._n
        length = self.v("length")
        if d >= length:
            return self.v("background")
        return C.lerp(self.v("background"), self.v("color"), 1 - d / length)


# --- sound-reactive (live only) ------------------------------------------------

class _AudioBase(Effect):
    live_only = True
    needs_audio = True
    _idle_period = 1 / 0.35

    def period(self):
        return self._idle_period

    def gain(self, v):
        return max(0.0, min(1.0, v * self.v("sensitivity") / 100.0))

    def band_of(self, key, reverse=False):
        pos = 1 - key.u if reverse else key.u
        return min(len(self.sound.bands) - 1, max(0, int(pos * len(self.sound.bands))))

    def idle(self, key, phase, a, b):
        """Gentle moving wave while nothing plays (a still board looks hung)."""
        f = (math.sin((key.u * 1.5 - phase) * TAU) + 1) / 2
        return C.scale(C.lerp(a, b, f), 0.10 + 0.16 * f)


def _sens():
    return Param("sensitivity", N_("Sensitivity (%)"), INT, 100, 20, 300, 5)


class AudioBars(_AudioBase):
    id, label = "equalizer", N_("Equalizer")
    description = N_("Frequency bars: bass on the left, treble on the right (system sound).")
    params = [Param("color", N_("Base color"), COLOR, "#00ff5a"),
              Param("peak", N_("Peak color"), COLOR, "#ff2800"),
              Param("background", N_("Background"), COLOR, "#040408"), _sens(),
              Param("reverse", N_("Reverse"), BOOL, False)]

    def color_for(self, key, t, phase):
        if not self.sound.live:
            return self.idle(key, phase, self.v("color"), self.v("peak"))
        level = self.gain(self.sound.bands[self.band_of(key, self.v("reverse"))])
        from_bottom = 1 - key.v
        if from_bottom > level:
            return self.v("background")
        return C.lerp(self.v("color"), self.v("peak"), from_bottom / max(level, 1e-3))


class AudioSpectrum(_AudioBase):
    id, label = "soundspectrum", N_("Sound spectrum")
    description = N_("The keyboard's rainbow lights up with each frequency band.")
    params = [_sens(), Param("saturation", N_("Saturation (%)"), INT, 100, 0, 100, 1),
              Param("reverse", N_("Reverse"), BOOL, False)]

    def color_for(self, key, t, phase):
        if not self.sound.live:
            return self.idle(key, phase, (255, 0, 80), (0, 120, 255))
        level = self.gain(self.sound.bands[self.band_of(key, self.v("reverse"))])
        return C.scale(C.hsv(key.u * 0.75 + phase * 0.05, self.v("saturation") / 100.0),
                       0.06 + 0.94 * level)


class AudioPulse(_AudioBase):
    id, label = "soundpulse", N_("Sound pulse")
    description = N_("The whole keyboard pulses on the beats (bass) of the music.")
    params = [Param("color", N_("Calm color"), COLOR, "#280050"),
              Param("peak", N_("Color on the beat"), COLOR, "#ff0078"), _sens()]

    def frame(self, t):
        if not self.sound.live:
            return super().frame(t)
        level = self.gain(self.sound.beat * 0.8 + self.sound.level * 0.2)
        c = C.scale(C.lerp(self.v("color"), self.v("peak"), level), 0.12 + 0.88 * level)
        out = [(0, 0, 0)] * NUM_LEDS
        for k in self.layout.keys:
            out[k.index] = c
        return out

    def color_for(self, key, t, phase):
        return self.idle(key, phase, self.v("color"), self.v("peak"))


class AudioWave(_AudioBase):
    id, label = "soundwave", N_("Music wave")
    description = N_("A two-color wave whose speed and brightness follow the volume.")
    params = [Param("color", N_("Color 1"), COLOR, "#00e5ff"),
              Param("peak", N_("Color 2"), COLOR, "#ff00c8"), speed_param(30),
              Param("direction", N_("Direction"), CHOICE, "right", choices=DIRECTIONS),
              _sens()]

    def setup(self):
        self._phase = 0.0
        self._last_t: float | None = None

    def frame(self, t):
        if not self.sound.live:
            self._last_t = t
            return super().frame(t)
        dt = 0.0 if self._last_t is None else max(0.0, min(0.2, t - self._last_t))
        self._last_t = t
        level = self.gain(self.sound.level)
        self._phase += dt * self.v("speed") / 30 * (0.15 + 1.2 * level)
        out = [(0, 0, 0)] * NUM_LEDS
        for k in self.layout.keys:
            pos = self.directional(k, self.v("direction"))
            f = (math.sin((pos - self._phase) * TAU) + 1) / 2
            out[k.index] = C.scale(C.lerp(self.v("color"), self.v("peak"), f), 0.08 + 0.92 * level)
        return out

    def color_for(self, key, t, phase):
        return self.idle(key, phase, self.v("color"), self.v("peak"))


# --- reactive layer: sits on top of any effect (live only) ----------------------

REACTIONS = {"none": N_("None"), "flash": N_("Light up the key"), "ripple": N_("Ripple")}
REACTION_PARAMS = [
    Param("type", N_("Key reaction"), CHOICE, "none", choices=REACTIONS),
    Param("color", N_("Reaction color"), COLOR, "#ffffff"),
    Param("rainbow", N_("Random color per key"), BOOL, False),
    Param("fade", N_("Duration (s)"), FLOAT, 0.8, 0.1, 5.0, 0.1),
    Param("velocity", N_("Wave speed"), FLOAT, 14.0, 2.0, 40.0, 1.0),
]


class ReactionLayer:
    """Additive key-press layer composited over the base effect."""

    def __init__(self, layout, values=None):
        self.layout = layout
        values = values or {}
        self.values = {p.name: p.coerce(values.get(p.name)) for p in REACTION_PARAMS}
        self._hits: Dict[int, float] = {}
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self.values["type"] != "none"

    def json_values(self):
        return {p.name: p.to_json(self.values[p.name]) for p in REACTION_PARAMS}

    def on_key(self, index, t):
        with self._lock:
            self._hits[index] = t

    def apply(self, out, t):
        if not self.active:
            return out
        v = self.values
        fade = v["fade"]
        with self._lock:
            for i in [i for i, h in self._hits.items() if t - h > fade]:
                del self._hits[i]
            hits = dict(self._hits)
        if not hits:
            return out

        def col(idx):
            return C.hsv(key_hash("rx", idx)) if v["rainbow"] else v["color"]

        if v["type"] == "flash":
            for idx, h in hits.items():
                if self.layout.key(idx) is not None:
                    out[idx] = C.lerp(out[idx], col(idx), 1 - (t - h) / fade)
            return out
        width = 1.5
        origins = [(self.layout.key(i), i, t - h) for i, h in hits.items() if self.layout.key(i)]
        for key in self.layout.keys:
            add = (0, 0, 0)
            for o, idx, age in origins:
                d = abs(math.hypot(key.cx - o.cx, (key.cy - o.cy) * 1.4) - v["velocity"] * age)
                if d < width:
                    k = (1 - d / width) * (1 - age / fade)
                    add = C.add(add, C.scale(col(idx), k))
            if add != (0, 0, 0):
                out[key.index] = C.add(out[key.index], add)
        return out


EFFECTS = [Static, Breathing, Spectrum, RainbowWave, ColorWave, Gradient, Scanner, Fire,
           Snake, Starlight, Rain, Flicker, Blink,
           AudioBars, AudioSpectrum, AudioPulse, AudioWave,
           Reactive, Ripple, Off]
REGISTRY = {cls.id: cls for cls in EFFECTS}
