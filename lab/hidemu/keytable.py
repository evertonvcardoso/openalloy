# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Physical key table of the Alloy Origins Core (tenkeyless), written for the lab.

Deliberately independent of ``openalloy.domain.layout``: the emulated keyboard
reports these HID usages (LampArray InputBinding), so the lab checks the
program's usage -> key mapping instead of reusing it.

Units: key units (1 u = 19.05 mm). Each entry: (name, hid_usage or 0, width_u).
``None`` entries are gaps. Counts: ABNT2 89 keys (matches the journal of the
real HP 03f0:098f: "89 LEDs, 89 mapped"), ANSI 87, ISO 88.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

U_MM = 19.05

Row = List[Optional[Tuple[str, int, float]]]

_FN_ROW: Row = [("esc", 0x29, 1), None, ("f1", 0x3A, 1), ("f2", 0x3B, 1), ("f3", 0x3C, 1),
                ("f4", 0x3D, 1), ("gap", 0, -0.5), ("f5", 0x3E, 1), ("f6", 0x3F, 1),
                ("f7", 0x40, 1), ("f8", 0x41, 1), ("gap", 0, -0.5), ("f9", 0x42, 1),
                ("f10", 0x43, 1), ("f11", 0x44, 1), ("f12", 0x45, 1), ("gap", 0, -0.25),
                ("prtsc", 0x46, 1), ("scrlk", 0x47, 1), ("pause", 0x48, 1)]

_NAV1 = [("gap", 0, -0.25), ("ins", 0x49, 1), ("home", 0x4A, 1), ("pgup", 0x4B, 1)]
_NAV2 = [("gap", 0, -0.25), ("del", 0x4C, 1), ("end", 0x4D, 1), ("pgdn", 0x4E, 1)]
_DIGITS = [(str(d), 0x1E + i, 1) for i, d in enumerate("1234567890")]
_QWERTY = [(c, u, 1) for c, u in zip("qwertyuiop", (0x14, 0x1A, 0x08, 0x15, 0x17, 0x1C,
                                                       0x18, 0x0C, 0x12, 0x13))]
_ASDF = [(c, u, 1) for c, u in zip("asdfghjkl", (0x04, 0x16, 0x07, 0x09, 0x0A, 0x0B, 0x0D,
                                                    0x0E, 0x0F))]
_ZXCV = [(c, u, 1) for c, u in zip("zxcvbnm", (0x1D, 0x1B, 0x06, 0x19, 0x05, 0x11, 0x10))]
_BOTTOM_LEFT = [("lctrl", 0xE0, 1.25), ("lwin", 0xE3, 1.25), ("lalt", 0xE2, 1.25),
                ("space", 0x2C, 6.25), ("ralt", 0xE6, 1.25), ("fn", 0, 1.25),
                ("menu", 0x65, 1.25), ("rctrl", 0xE4, 1.25)]
_ARROWS_BOTTOM = [("gap", 0, -0.25), ("left", 0x50, 1), ("down", 0x51, 1), ("right", 0x4F, 1)]


def _rows(variant: str) -> List[Row]:
    if variant == "abnt2":
        r1 = [("quote", 0x35, 1)] + _DIGITS + [("minus", 0x2D, 1), ("equal", 0x2E, 1),
                                                ("backspace", 0x2A, 2)] + _NAV1
        r2 = [("tab", 0x2B, 1.5)] + _QWERTY + [("acute", 0x2F, 1), ("lbracket", 0x30, 1),
                                                ("enter", 0x28, 1.5)] + _NAV2
        r3 = [("caps", 0x39, 1.75)] + _ASDF + [("cedilla", 0x33, 1), ("tilde", 0x34, 1),
                                               ("rbracket", 0x32, 1)]
        r4 = ([("lshift", 0xE1, 1.25), ("backslash", 0x64, 1)] + _ZXCV
              + [("comma", 0x36, 1), ("period", 0x37, 1), ("semicolon", 0x38, 1),
                 ("slash", 0x87, 1), ("rshift", 0xE5, 1.75), ("gap", 0, -1.25),
                 ("up", 0x52, 1)])
    elif variant == "iso":
        r1 = [("grave", 0x35, 1)] + _DIGITS + [("minus", 0x2D, 1), ("equal", 0x2E, 1),
                                               ("backspace", 0x2A, 2)] + _NAV1
        r2 = [("tab", 0x2B, 1.5)] + _QWERTY + [("lbracket", 0x2F, 1), ("rbracket", 0x30, 1),
                                                ("enter", 0x28, 1.5)] + _NAV2
        r3 = [("caps", 0x39, 1.75)] + _ASDF + [("semicolon", 0x33, 1), ("quote", 0x34, 1),
                                               ("hash", 0x32, 1)]
        r4 = ([("lshift", 0xE1, 1.25), ("backslash", 0x64, 1)] + _ZXCV
              + [("comma", 0x36, 1), ("period", 0x37, 1), ("slash", 0x38, 1),
                 ("rshift", 0xE5, 2.75), ("gap", 0, -1.25), ("up", 0x52, 1)])
    elif variant == "ansi":
        r1 = [("grave", 0x35, 1)] + _DIGITS + [("minus", 0x2D, 1), ("equal", 0x2E, 1),
                                               ("backspace", 0x2A, 2)] + _NAV1
        r2 = [("tab", 0x2B, 1.5)] + _QWERTY + [("lbracket", 0x2F, 1), ("rbracket", 0x30, 1),
                                                ("backslash", 0x31, 1.5)] + _NAV2
        r3 = [("caps", 0x39, 1.75)] + _ASDF + [("semicolon", 0x33, 1), ("quote", 0x34, 1),
                                               ("enter", 0x28, 2.25)]
        r4 = ([("lshift", 0xE1, 2.25)] + _ZXCV
              + [("comma", 0x36, 1), ("period", 0x37, 1), ("slash", 0x38, 1),
                 ("rshift", 0xE5, 2.75), ("gap", 0, -1.25), ("up", 0x52, 1)])
    else:
        raise ValueError(f"unknown variant {variant!r}")
    r4_fixed = [k for k in r4]
    return [_FN_ROW, r1, r2, r3, r4_fixed, _BOTTOM_LEFT + _ARROWS_BOTTOM]


@dataclass(frozen=True)
class Key:
    name: str
    usage: int          # HID keyboard usage; 0 = none (Fn)
    row: int
    x: float            # left edge, key units
    y: float            # top edge, key units
    w: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + 0.5


def keys(variant: str) -> List[Key]:
    out: List[Key] = []
    for r, row in enumerate(_rows(variant)):
        x = 0.0
        y = 0.0 if r == 0 else r + 0.25          # the function row stands apart
        for item in row:
            if item is None:
                x += 1.0                           # gap after Esc
                continue
            name, usage, w = item
            if name == "gap":
                x += -w if w < 0 else w
                if w < 0:
                    x += 0.0
                continue
            out.append(Key(name, usage, r, x, y, w))
            x += w
    # place the up arrow above the down arrow and the nav cluster in its column
    by = {k.name: k for k in out}
    if "down" in by and "up" in by:
        up = by["up"]
        out[out.index(up)] = Key("up", up.usage, up.row, by["down"].x, up.y, 1)
    return out


WIDTH_U = 18.25
HEIGHT_U = 6.5


def by_usage(variant: str) -> Dict[int, Key]:
    return {k.usage: k for k in keys(variant) if k.usage}


EXPECTED_COUNT = {"abnt2": 89, "ansi": 87, "iso": 88}
