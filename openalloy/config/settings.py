# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Persistent settings and presets.

Merges Linuity's atomic tempfile+os.replace writes with PlasmaNGenuity's
JSON defaults + deep-merge. Unlike Linuity's flat key=value preset (read by a
root daemon from the user's home), everything lives in the user's XDG dirs
and the daemon runs as the user.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from openalloy.i18n import N_, Message

log = logging.getLogger(__name__)

DEFAULTS: Dict[str, Any] = {
    "version": 1,
    "mode": "live",                 # "live" (daemon streams) or "onboard"
    "lighting": {"effect": "wave", "params": {}, "keys": {}},
    "fps": 30,
    "layout_override": None,        # None = auto-detect, or ansi/abnt2/iso
    "profile_key_action": "release",  # release | bind | ignore
    "profile_bindings": {"1": None, "2": None, "3": None},
    "reactive_input": True,         # read key presses for reactive effects
    "restore_brightness": None,     # 0-255 to apply when the daemon connects
    "onboard_frames": 60,
    "onboard_keypress": "match",    # match | black | keep | custom
    "onboard_keypress_color": "#ffffff",
    "language": "en",               # interface language: en (default) or pt_BR
    "led_gamma": 2.2,               # LED colour correction (1.0 = send colours raw)
    "software_brightness": None,    # LampArray has no hardware brightness: kept here
    "audio_source": "",             # "" = monitor of the default output
    "flash_validated": {},          # keyboards where live RGB was confirmed (see flashgate)
}

MODES = ("live", "onboard", "fixed")
LAYOUTS = ("ansi", "abnt2", "iso")
KEYPRESS_MODES = ("match", "black", "keep", "custom")
PROFILE_KEY_ACTIONS = ("release", "bind", "ignore")


def _int_in(lo: int, hi: int):
    def check(v):
        n = int(round(_number(v)))
        if not lo <= n <= hi:
            raise ValueError(Message(N_("must be between {low} and {high}"), low=lo, high=hi))
        return n
    return check


def _optional(check):
    return lambda v: None if v is None else check(v)


def _one_of(choices):
    def check(v):
        if v not in choices:
            raise ValueError(Message(N_("use one of: {choices}"),
                choices=", ".join(str(c) for c in choices)))
        return v
    return check


def _bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("1", "true", "sim", "yes", "on", "0",
                                                    "false", "nao", "não", "no", "off"):
        return v.strip().lower() in ("1", "true", "sim", "yes", "on")
    if isinstance(v, int):
        return bool(v)
    raise ValueError(Message(N_("true or false is expected")))


def _number(v) -> float:
    """float(v), with our own message ("abc" gave Python's untranslated text)."""
    import math
    if isinstance(v, bool):
        raise ValueError(Message(N_("a number is expected")))
    try:
        n = float(v)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(Message(N_("a number is expected"))) from None
    if not math.isfinite(n):
        raise ValueError(Message(N_("a number is expected")))
    return n


def _gamma(v):
    g = _number(v)
    if not 1.0 <= g <= 3.0:
        raise ValueError(Message(N_("must be between {low} and {high}"), low="1.0", high="3.0"))
    return g


def _color(v):
    from openalloy.domain.colors import parse_color, to_hex
    return to_hex(parse_color(v))


def _bindings(v):
    if not isinstance(v, dict):
        raise ValueError(Message(N_("an object like {example} is expected"),
            example='{"1": preset, ...}'))
    out = {}
    for p in ("1", "2", "3"):
        b = v.get(p, v.get(int(p)) if isinstance(v, dict) else None)
        out[p] = str(b) if b not in (None, "") else None
    return out


def _string(v):
    if not isinstance(v, str):
        raise ValueError(Message(N_("text is expected")))
    return v.strip()


def _language(v):
    from openalloy.i18n import LANGUAGES, normalize
    code = normalize(v)
    if code is None:
        raise ValueError(Message(N_("use one of: {choices}"), choices=", ".join(LANGUAGES)))
    return code


# One validator per setting a user (GUI, CLI, RPC or a hand edit) may change.
VALIDATORS = {
    "mode": _one_of(MODES),
    "fps": _int_in(5, 60),
    "layout_override": _optional(_one_of(LAYOUTS)),
    "profile_key_action": _one_of(PROFILE_KEY_ACTIONS),
    "profile_bindings": _bindings,
    "reactive_input": _bool,
    "restore_brightness": _optional(_int_in(0, 255)),
    "software_brightness": _optional(_int_in(0, 255)),
    "onboard_frames": _int_in(1, 255),
    "onboard_keypress": _one_of(KEYPRESS_MODES),
    "onboard_keypress_color": _color,
    "led_gamma": _gamma,
    "audio_source": _string,
    "language": _language,
}


def validate(key: str, value: Any) -> Any:
    """Return the cleaned value or raise ValueError with a readable message."""
    check = VALIDATORS.get(key)
    if check is None:
        return value
    try:
        return check(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(Message(N_("{key}: invalid value {value!r} ({reason})"),
            key=key, value=value, reason=exc)) from None


# Hand-edited or foreign files are bounded before parsing (memory, recursion).
MAX_CONFIG_BYTES = 1024 * 1024
MAX_PRESET_BYTES = 256 * 1024


def read_json_bounded(path: Path, limit: int) -> Any:
    """json.load with a size limit; RecursionError (absurd nesting) becomes ValueError."""
    with open(path, "rb") as fh:
        raw = fh.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"file larger than {limit} bytes")
    try:
        return json.loads(raw.decode("utf-8"))
    except RecursionError:
        raise ValueError("JSON nested too deeply") from None


def _clean_lighting(light: dict) -> dict:
    keys = light.get("keys")
    if isinstance(keys, dict):
        good = {}
        for k, v in keys.items():
            if str(k).isdigit() and int(k) < 94 and isinstance(v, (str, list)):
                good[str(int(k))] = v
        light["keys"] = good
    elif keys is not None:
        light["keys"] = {}
    for part in ("params", "reaction"):
        if part in light and not isinstance(light[part], dict):
            light[part] = {}
    return light


def sanitize(data: dict) -> dict:
    """Replace every invalid value (e.g. from a hand-edited file) by its default."""
    for key in VALIDATORS:
        try:
            data[key] = validate(key, data.get(key, DEFAULTS[key]))
        except ValueError as exc:
            log.warning("Config: %s; using the default", exc)
            data[key] = copy.deepcopy(DEFAULTS[key])
    for key in ("lighting", "flash_validated"):
        if not isinstance(data.get(key), dict):
            log.warning("Config: invalid %s; using the default", key)
            data[key] = copy.deepcopy(DEFAULTS[key])
    data["lighting"] = _clean_lighting(data["lighting"])
    records = data["flash_validated"]
    good = {k: v for k, v in records.items()
            if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("firmware"), str)}
    if len(good) != len(records):
        log.warning("Config: %d invalid validation record(s) dropped", len(records) - len(good))
    data["flash_validated"] = good
    return data


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / "openalloy"


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        return Path(base)
    return Path(tempfile.gettempdir()) / f"openalloy-{os.getuid()}"


def socket_path() -> Path:
    return Path(os.environ.get("OPENALLOY_SOCKET") or runtime_dir() / "openalloy.sock")


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Settings:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else config_dir() / "config.json"
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            user = read_json_bounded(self.path, MAX_CONFIG_BYTES)
            if not isinstance(user, dict):
                raise ValueError("the root is not an object")
            return sanitize(deep_merge(DEFAULTS, user))
        except FileNotFoundError:
            return copy.deepcopy(DEFAULTS)
        except (OSError, ValueError) as exc:
            log.warning("Invalid config in %s (%s); using defaults", self.path, exc)
            return copy.deepcopy(DEFAULTS)

    def reload(self) -> None:
        with self._lock:
            self._data = self._load()

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    def update(self, **values: Any) -> None:
        clean = {k: validate(k, v) for k, v in values.items()}   # all-or-nothing
        with self._lock:
            for k, v in clean.items():
                self._data[k] = copy.deepcopy(v)
            self.save()

    def save(self) -> None:
        with self._lock:
            atomic_write_json(self.path, self._data)

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)


# --- presets ---------------------------------------------------------------

def presets_dir() -> Path:
    return config_dir() / "presets"


def builtin_presets_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "resources" / "presets"


def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip().lower()).strip("-")
    return s or "preset"


class PresetStore:
    def __init__(self, user_dir: Optional[Path] = None, builtin_dir: Optional[Path] = None):
        self.user_dir = Path(user_dir) if user_dir else presets_dir()
        self.builtin_dir = Path(builtin_dir) if builtin_dir else builtin_presets_dir()

    def _scan(self, directory: Path, builtin: bool) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        if not directory.is_dir():
            return out
        for f in sorted(directory.glob("*.json")):
            try:
                data = read_json_bounded(f, MAX_PRESET_BYTES)
                if not isinstance(data, dict):
                    raise ValueError("the file does not hold a JSON object")
                lighting = data.get("lighting", data)
                if not isinstance(lighting, dict) or not isinstance(lighting.get("effect"), str):
                    raise ValueError("no 'lighting' with an 'effect'")
                name = data.get("name")
                out[f.stem] = {"id": f.stem, "name": name if isinstance(name, str) and name
                               else f.stem, "lighting": lighting, "builtin": builtin,
                               "path": str(f)}
            except (OSError, ValueError) as exc:
                log.warning("Preset skipped %s: %s", f, exc)
        return out

    def list(self) -> List[dict]:
        merged = self._scan(self.builtin_dir, True)
        merged.update(self._scan(self.user_dir, False))
        return sorted(merged.values(), key=lambda p: (not p["builtin"], p["name"].lower()))

    def get(self, ident: str) -> Optional[dict]:
        if not isinstance(ident, str) or not ident.strip():
            return None
        from openalloy.i18n import pgettext
        ident_l = ident.lower()
        for p in self.list():
            shown = pgettext("preset", p["name"]) if p["builtin"] else p["name"]
            if p["id"] == ident or p["id"] == slugify(ident) or ident_l in (
                    p["name"].lower(), shown.lower()):
                return p
        return None

    def save(self, name: str, lighting: dict) -> str:
        if not isinstance(name, str) or not name.strip():
            raise ValueError(Message(N_("Give the preset a name.")))
        if not isinstance(lighting, dict):
            raise ValueError(Message(N_("Invalid lighting.")))
        name = name.strip()
        ident = slugify(name)
        atomic_write_json(self.user_dir / f"{ident}.json", {"name": name, "lighting": lighting})
        return ident

    def delete(self, ident: str) -> bool:
        if not isinstance(ident, str) or not ident.strip():
            return False
        p = self.user_dir / f"{slugify(ident)}.json"
        try:
            p.unlink()                     # no exists() check first: that races
            return True
        except FileNotFoundError:
            return False
