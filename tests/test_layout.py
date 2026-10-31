# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
import pytest

from openalloy.domain.layout import (
    DEAD_ANSI,
    HID_USAGE,
    INDEX_BY_HID_USAGE,
    NUM_LEDS,
    get_layout,
    variant_from_byte,
)

# (row, col) -> LED value, as produced by OpenRGB's KeyboardLayoutManager for
# KEYBOARD_SIZE_TKL with hyperx_core_default_values (ANSI QWERTY).
OPENRGB_ANSI = {
    "escape": 0, "f1": 1, "f7": 7, "f8": 48, "f12": 52, "pause": 55, "grave": 8, "8": 16,
    "9": 56, "backspace": 60, "pageup": 63, "tab": 17, "u": 24, "i": 64, "backslash": 69,
    "pagedown": 72, "capslock": 25, "j": 32, "k": 73, "quote": 76, "enter": 78,
    "leftshift": 33, "z": 35, "n": 40, "m": 79, "slash": 82, "rightshift": 84, "up": 85,
    "leftctrl": 41, "leftmeta": 42, "leftalt": 43, "space": 45, "rightalt": 86, "fn": 87,
    "menu": 88, "rightctrl": 89, "left": 90, "down": 91, "right": 92,
}


def test_key_counts_match_openrgb():
    assert len(get_layout("ansi").keys) == 87
    assert len(get_layout("abnt2").keys) == 89
    assert len(get_layout("iso").keys) == 88


def test_ansi_indices_match_openrgb():
    lay = get_layout("ansi")
    for name, idx in OPENRGB_ANSI.items():
        assert lay.by_name(name).index == idx, name


def test_ansi_dead_indices_are_unlit():
    lay = get_layout("ansi")
    assert not set(DEAD_ANSI) & set(lay.indices)
    assert set(lay.indices) | set(DEAD_ANSI) == set(range(NUM_LEDS))


def test_abnt2_overlay():
    lay = get_layout("abnt2")
    assert lay.key(69) is None                      # no ANSI backslash
    assert lay.key(34).label == "\\"                # ISO extra key
    assert lay.key(75).label == "Ç"
    assert lay.key(83).label == "/" and lay.key(83).evdev == 89  # KEY_RO
    assert lay.key(82).label == ";"
    assert lay.key(78).extra is not None            # L-shaped Enter


@pytest.mark.parametrize("variant", ["ansi", "abnt2", "iso"])
def test_no_overlapping_keys_and_unique_evdev(variant):
    lay = get_layout(variant)
    rows = {}
    for k in lay.keys:
        rows.setdefault(k.row, []).append(k)
    for keys in rows.values():
        keys.sort(key=lambda k: k.x)
        for a, b in zip(keys, keys[1:]):
            assert a.x + a.w <= b.x + 1e-9, (a.name, b.name)
    codes = [k.evdev for k in lay.keys if k.evdev is not None]
    assert len(codes) == len(set(codes))


def test_resolve_and_groups():
    lay = get_layout("ansi")
    assert lay.resolve("W") == 19 and lay.resolve(19) == 19 and lay.resolve("19") == 19
    assert lay.resolve("nope") is None and lay.resolve(200) is None


def test_hid_usage_table():
    assert INDEX_BY_HID_USAGE[HID_USAGE[19]] == 19
    assert HID_USAGE[26] == 4  # 'a'


def test_variant_byte():
    assert variant_from_byte(0x09) == "ansi"
    assert variant_from_byte(0x10) == "abnt2"
    assert variant_from_byte(0x77) is None


# --- US (ANSI) keyboards next to the ABNT2 one ------------------------------------


@pytest.mark.parametrize("variant,keys", [("ansi", 87), ("abnt2", 89), ("iso", 88)])
def test_hp_lamparray_detects_each_layout(monkeypatch, variant, keys):
    from openalloy.infra.drivers import open_driver
    monkeypatch.setenv("OPENALLOY_SIM_LAYOUT", variant)
    drv = open_driver("hp")
    assert drv.info().variant == variant and len(drv.lamp_map) == keys


@pytest.mark.parametrize("variant,expected", [("ansi", "ansi"), ("abnt2", "abnt2")])
def test_vendor_channel_reports_the_layout_byte(monkeypatch, variant, expected):
    from openalloy.infra.drivers import open_driver
    monkeypatch.setenv("OPENALLOY_SIM_LAYOUT", variant)
    assert open_driver("core").info().variant == expected


def test_us_layout_keys_light_their_own_leds(monkeypatch):
    from openalloy.infra.lamparray import LampArrayDriver
    from openalloy.infra.simulator import SimulatedLampArray, SimulatedLampTransport
    arr = SimulatedLampArray("ansi")
    drv = LampArrayDriver(SimulatedLampTransport(arr), simulated=True)
    layout = get_layout("ansi")
    frame = [(0, 0, 0)] * 94
    for name in ("backslash", "enter", "leftshift", "quote"):     # keys that differ from ABNT2
        frame[layout.by_name(name).index] = (255, 255, 255)
    drv.stream(frame)
    lit = {i for i, c in arr.key_colors().items() if c == (255, 255, 255)}
    assert lit == {layout.by_name(n).index for n in ("backslash", "enter", "leftshift", "quote")}
