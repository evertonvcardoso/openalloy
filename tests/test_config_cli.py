# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
import json

import pytest

from openalloy.application.effects import Lighting, normalize
from openalloy.config.settings import DEFAULTS, PresetStore, Settings, deep_merge
from openalloy.domain.layout import get_layout
from openalloy.presentation.cli.main import build_parser, parse_keys, parse_level


def test_deep_merge_keeps_defaults():
    merged = deep_merge(DEFAULTS, {"profile_bindings": {"2": "x"}, "fps": 50})
    assert merged["profile_bindings"] == {"1": None, "2": "x", "3": None}
    assert merged["fps"] == 50 and merged["mode"] == "live"


def test_settings_atomic_and_tolerant(tmp_path):
    p = tmp_path / "c.json"
    s = Settings(p)
    s.update(fps=12)
    assert json.loads(p.read_text())["fps"] == 12
    p.write_text("{broken")
    assert Settings(p).get("fps") == DEFAULTS["fps"]
    assert not list(tmp_path.glob(".tmp-*"))


def test_presets_crud(tmp_path):
    store = PresetStore(tmp_path / "u")
    ident = store.save("Meu Preset!", {"effect": "static", "params": {}, "keys": {}})
    assert ident == "meu-preset"
    assert store.get("Meu Preset!")["builtin"] is False
    assert store.delete(ident) and store.get(ident) is None


@pytest.mark.parametrize("variant", ["ansi", "abnt2", "iso"])
def test_builtin_presets_are_valid(variant):
    presets = PresetStore().list()
    assert len(presets) >= 10
    for p in presets:
        normalize(Lighting.from_dict(p["lighting"]), get_layout(variant))


def test_parse_keys_groups_names_and_indices():
    keys = parse_keys(["wasd=red", "19=#00ff00", "space=blue"], "ansi")
    assert keys["19"] == "#00ff00" and keys["26"] == "#ff0000" and keys["45"] == "#0000ff"
    with pytest.raises(SystemExit):
        parse_keys(["nokey=red"], "ansi")


def test_parse_level():
    assert parse_level("50%", 0) == 128
    assert parse_level("+", 128) == 192 and parse_level("-", 128) == 64
    assert parse_level("0x40", 0) == 64


def test_parser_accepts_documented_commands():
    ap = build_parser()
    ap.parse_args(["apply", "wave", "-s", "40", "-k", "wasd=white"])
    ap.parse_args(["onboard", "write", "2", "starlight", "--frames", "80", "-y"])
    ap.parse_args(["--simulate", "status"])
