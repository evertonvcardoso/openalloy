# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Physical layout of the HyperX Alloy Origins Core (tenkeyless).

The keyboard always receives 94 LED colours. Indices run in hardware scan
order: left half first (0-47), then the right half (48-93), row by row.
Mapping verified against OpenRGB's KeyboardLayoutManager output for
KEYBOARD_SIZE_TKL (ANSI and ABNT2 overlays) and hxoctkl's keymap.

Seven indices have no key on ANSI: 34, 44, 46, 47, 77, 83, 93. On ISO/ABNT2
some of them become real keys (34 = ISO extra key, 77 = ISO #, 83 = ABNT2 /?).

Geometry is expressed in key units (1u = one alphanumeric key). ``x`` is the
left edge, ``y`` the top edge. The ISO/ABNT2 Enter is drawn as an L shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

NUM_LEDS = 94
DEAD_ANSI = (34, 44, 46, 47, 77, 83, 93)

VARIANT_ANSI = "ansi"
VARIANT_ABNT2 = "abnt2"
VARIANT_ISO = "iso"
VARIANTS = (VARIANT_ANSI, VARIANT_ABNT2, VARIANT_ISO)

# Byte 56 of the 0x10 device-info reply.
VARIANT_BYTES = {0x09: VARIANT_ANSI, 0x10: VARIANT_ABNT2}

ROW_Y = (0.0, 1.3, 2.3, 3.3, 4.3, 5.3)
BOARD_WIDTH = 18.25
BOARD_HEIGHT = 6.3


@dataclass(frozen=True)
class Key:
    index: int           # LED index 0-93
    name: str            # stable id, e.g. "w", "leftshift"
    label: str           # text printed on the keycap
    row: int
    x: float             # left edge (units)
    w: float             # width (units)
    evdev: Optional[int]  # Linux input keycode (None for Fn)
    h: float = 1.0
    # Optional extra rectangle (x, y, w, h) for the ISO Enter L shape.
    extra: Optional[Tuple[float, float, float, float]] = None

    @property
    def y(self) -> float:
        return ROW_Y[self.row]

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def u(self) -> float:
        """Normalised horizontal centre, 0..1."""
        return self.cx / BOARD_WIDTH

    @property
    def v(self) -> float:
        """Normalised vertical centre, 0..1."""
        return self.cy / BOARD_HEIGHT


# (index, name, label, row, x, w, evdev)
_ANSI: List[tuple] = [
    # Row 0 - function row
    (0, "escape", "Esc", 0, 0.0, 1, 1),
    (1, "f1", "F1", 0, 2.0, 1, 59), (2, "f2", "F2", 0, 3.0, 1, 60),
    (3, "f3", "F3", 0, 4.0, 1, 61), (4, "f4", "F4", 0, 5.0, 1, 62),
    (5, "f5", "F5", 0, 6.5, 1, 63), (6, "f6", "F6", 0, 7.5, 1, 64),
    (7, "f7", "F7", 0, 8.5, 1, 65), (48, "f8", "F8", 0, 9.5, 1, 66),
    (49, "f9", "F9", 0, 11.0, 1, 67), (50, "f10", "F10", 0, 12.0, 1, 68),
    (51, "f11", "F11", 0, 13.0, 1, 87), (52, "f12", "F12", 0, 14.0, 1, 88),
    (53, "printscreen", "PrtSc", 0, 15.25, 1, 99),
    (54, "scrolllock", "ScrLk", 0, 16.25, 1, 70),
    (55, "pause", "Pause", 0, 17.25, 1, 119),
    # Row 1 - numbers
    (8, "grave", "`", 1, 0.0, 1, 41),
    (9, "1", "1", 1, 1.0, 1, 2), (10, "2", "2", 1, 2.0, 1, 3),
    (11, "3", "3", 1, 3.0, 1, 4), (12, "4", "4", 1, 4.0, 1, 5),
    (13, "5", "5", 1, 5.0, 1, 6), (14, "6", "6", 1, 6.0, 1, 7),
    (15, "7", "7", 1, 7.0, 1, 8), (16, "8", "8", 1, 8.0, 1, 9),
    (56, "9", "9", 1, 9.0, 1, 10), (57, "0", "0", 1, 10.0, 1, 11),
    (58, "minus", "-", 1, 11.0, 1, 12), (59, "equal", "=", 1, 12.0, 1, 13),
    (60, "backspace", "Backspace", 1, 13.0, 2, 14),
    (61, "insert", "Ins", 1, 15.25, 1, 110), (62, "home", "Home", 1, 16.25, 1, 102),
    (63, "pageup", "PgUp", 1, 17.25, 1, 104),
    # Row 2 - QWERTY
    (17, "tab", "Tab", 2, 0.0, 1.5, 15),
    (18, "q", "Q", 2, 1.5, 1, 16), (19, "w", "W", 2, 2.5, 1, 17),
    (20, "e", "E", 2, 3.5, 1, 18), (21, "r", "R", 2, 4.5, 1, 19),
    (22, "t", "T", 2, 5.5, 1, 20), (23, "y", "Y", 2, 6.5, 1, 21),
    (24, "u", "U", 2, 7.5, 1, 22), (64, "i", "I", 2, 8.5, 1, 23),
    (65, "o", "O", 2, 9.5, 1, 24), (66, "p", "P", 2, 10.5, 1, 25),
    (67, "leftbracket", "[", 2, 11.5, 1, 26), (68, "rightbracket", "]", 2, 12.5, 1, 27),
    (69, "backslash", "\\", 2, 13.5, 1.5, 43),
    (70, "delete", "Del", 2, 15.25, 1, 111), (71, "end", "End", 2, 16.25, 1, 107),
    (72, "pagedown", "PgDn", 2, 17.25, 1, 109),
    # Row 3 - home row
    (25, "capslock", "Caps", 3, 0.0, 1.75, 58),
    (26, "a", "A", 3, 1.75, 1, 30), (27, "s", "S", 3, 2.75, 1, 31),
    (28, "d", "D", 3, 3.75, 1, 32), (29, "f", "F", 3, 4.75, 1, 33),
    (30, "g", "G", 3, 5.75, 1, 34), (31, "h", "H", 3, 6.75, 1, 35),
    (32, "j", "J", 3, 7.75, 1, 36), (73, "k", "K", 3, 8.75, 1, 37),
    (74, "l", "L", 3, 9.75, 1, 38), (75, "semicolon", ";", 3, 10.75, 1, 39),
    (76, "quote", "'", 3, 11.75, 1, 40),
    (78, "enter", "Enter", 3, 12.75, 2.25, 28),
    # Row 4 - bottom letters
    (33, "leftshift", "Shift", 4, 0.0, 2.25, 42),
    (35, "z", "Z", 4, 2.25, 1, 44), (36, "x", "X", 4, 3.25, 1, 45),
    (37, "c", "C", 4, 4.25, 1, 46), (38, "v", "V", 4, 5.25, 1, 47),
    (39, "b", "B", 4, 6.25, 1, 48), (40, "n", "N", 4, 7.25, 1, 49),
    (79, "m", "M", 4, 8.25, 1, 50), (80, "comma", ",", 4, 9.25, 1, 51),
    (81, "period", ".", 4, 10.25, 1, 52), (82, "slash", "/", 4, 11.25, 1, 53),
    (84, "rightshift", "Shift", 4, 12.25, 2.75, 54),
    (85, "up", "↑", 4, 16.25, 1, 103),
    # Row 5 - modifiers
    (41, "leftctrl", "Ctrl", 5, 0.0, 1.25, 29),
    (42, "leftmeta", "Super", 5, 1.25, 1.25, 125),
    (43, "leftalt", "Alt", 5, 2.5, 1.25, 56),
    (45, "space", "", 5, 3.75, 6.25, 57),
    (86, "rightalt", "AltGr", 5, 10.0, 1.25, 100),
    (87, "fn", "Fn", 5, 11.25, 1.25, None),
    (88, "menu", "Menu", 5, 12.5, 1.25, 127),
    (89, "rightctrl", "Ctrl", 5, 13.75, 1.25, 97),
    (90, "left", "←", 5, 15.25, 1, 105),
    (91, "down", "↓", 5, 16.25, 1, 108),
    (92, "right", "→", 5, 17.25, 1, 106),
]

# ISO family changes relative to ANSI: index -> (name, label, row, x, w, evdev)
_ISO_COMMON = {
    # Row 2: no ANSI backslash; Enter top part occupies 13.5..15
    69: None,
    # Row 3: key 77 becomes ISO hash, Enter is the L-shaped ISO Enter
    77: ("nonushash", "#", 3, 12.75, 1, 43),
    # Row 4: short left shift + extra key at 34
    33: ("leftshift", "Shift", 4, 0.0, 1.25, 42),
    34: ("102nd", "\\", 4, 1.25, 1, 86),
}

_ABNT2_LABELS = {
    8: "'", 67: "´", 68: "[", 75: "Ç", 76: "~", 77: "]",
    34: "\\", 82: ";", 83: "/",
}
_ISO_LABELS = {8: "`", 77: "#", 34: "\\"}


@dataclass
class Layout:
    variant: str
    keys: List[Key] = field(default_factory=list)

    def __post_init__(self):
        self._by_index: Dict[int, Key] = {k.index: k for k in self.keys}
        self._by_name: Dict[str, Key] = {k.name: k for k in self.keys}
        self._by_evdev: Dict[int, Key] = {k.evdev: k for k in self.keys if k.evdev is not None}

    def key(self, index: int) -> Optional[Key]:
        return self._by_index.get(index)

    def by_name(self, name: str) -> Optional[Key]:
        return self._by_name.get(name.lower())

    def by_evdev(self, code: int) -> Optional[Key]:
        return self._by_evdev.get(code)

    @property
    def indices(self) -> List[int]:
        return sorted(self._by_index)

    def resolve(self, ref) -> Optional[int]:
        """Accept an LED index (int or numeric string) or a key name."""
        if isinstance(ref, int):
            return ref if 0 <= ref < NUM_LEDS else None
        s = str(ref).strip().lower()
        if s.isdigit():
            i = int(s)
            return i if 0 <= i < NUM_LEDS else None
        k = self.by_name(s)
        return k.index if k else None


def _build(variant: str) -> Layout:
    rows = {r[0]: r[1:] for r in _ANSI}
    labels: Dict[int, str] = {}
    if variant in (VARIANT_ABNT2, VARIANT_ISO):
        for idx, spec in _ISO_COMMON.items():
            if spec is None:
                rows.pop(idx, None)
            else:
                rows[idx] = spec
        # Row 3 keys between L and Enter keep ANSI positions (A..quote shift
        # nothing); Enter becomes ISO.
        rows.pop(78, None)
        if variant == VARIANT_ABNT2:
            # ABNT2: extra "/?" key (83) next to right shift, shorter shift.
            rows[82] = ("slash", ";", 4, 11.25, 1, 53)
            rows[83] = ("ro", "/", 4, 12.25, 1, 89)
            rows[84] = ("rightshift", "Shift", 4, 13.25, 1.75, 54)
            labels = _ABNT2_LABELS
        else:
            labels = _ISO_LABELS

    keys = []
    for idx, (name, label, row, x, w, ev) in rows.items():
        keys.append(Key(idx, name, labels.get(idx, label), row, x, w, ev))
    if variant in (VARIANT_ABNT2, VARIANT_ISO):
        keys.append(Key(78, "enter", "Enter", 2, 13.5, 1.5, 28,
                        h=1.0, extra=(13.75, ROW_Y[3], 1.25, 1.0)))
    keys.sort(key=lambda k: k.index)
    return Layout(variant, keys)


_CACHE: Dict[str, Layout] = {}


def get_layout(variant: str = VARIANT_ANSI) -> Layout:
    variant = variant if variant in VARIANTS else VARIANT_ANSI
    if variant not in _CACHE:
        _CACHE[variant] = _build(variant)
    return _CACHE[variant]


def variant_from_byte(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    return VARIANT_BYTES.get(value)


# HID usage (keyboard page) per LED index; used to decode the firmware's
# unsolicited 0x73 key reports. 34/77/83 only exist on ISO/ABNT2 boards.
HID_USAGE = {
    0: 41, 1: 58, 2: 59, 3: 60, 4: 61, 5: 62, 6: 63, 7: 64, 8: 53, 9: 30, 10: 31,
    11: 32, 12: 33, 13: 34, 14: 35, 15: 36, 16: 37, 17: 43, 18: 20, 19: 26, 20: 8,
    21: 21, 22: 23, 23: 28, 24: 24, 25: 57, 26: 4, 27: 22, 28: 7, 29: 9, 30: 10,
    31: 11, 32: 13, 33: 225, 34: 100, 35: 29, 36: 27, 37: 6, 38: 25, 39: 5, 40: 17,
    41: 224, 42: 227, 43: 226, 45: 44, 48: 65, 49: 66, 50: 67, 51: 68, 52: 69,
    53: 70, 54: 71, 55: 72, 56: 38, 57: 39, 58: 45, 59: 46, 60: 42, 61: 73, 62: 74,
    63: 75, 64: 12, 65: 18, 66: 19, 67: 47, 68: 48, 69: 49, 70: 76, 71: 77, 72: 78,
    73: 14, 74: 15, 75: 51, 76: 52, 77: 50, 78: 40, 79: 16, 80: 54, 81: 55, 82: 56,
    83: 135, 84: 229, 85: 82, 86: 230, 87: 232, 88: 101, 89: 228, 90: 80, 91: 81,
    92: 79,
}
INDEX_BY_HID_USAGE = {u: i for i, u in HID_USAGE.items()}


# Named key groups usable in presets and the GUI quick-select.
GROUPS = {
    "wasd": ["w", "a", "s", "d"],
    "arrows": ["up", "down", "left", "right"],
    "frow": ["escape"] + [f"f{i}" for i in range(1, 13)],
    "numbers": ["grave"] + [str(i) for i in range(10)] + ["minus", "equal"],
    "letters": list("qwertyuiopasdfghjklzxcvbnm"),
    "modifiers": ["leftshift", "rightshift", "leftctrl", "rightctrl", "leftalt",
                  "rightalt", "leftmeta", "fn", "menu", "capslock", "tab"],
    "nav": ["printscreen", "scrolllock", "pause", "insert", "home", "pageup",
            "delete", "end", "pagedown"],
}
