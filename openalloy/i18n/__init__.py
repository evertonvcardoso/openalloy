# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Internationalisation (GNU gettext, standard library only).

* Source language: English. Every user-facing message in the code is written in
  English and wrapped in ``_()`` / ``ngettext()`` / ``pgettext()``; ``N_()``
  only marks a string for extraction (translated later, at display time).
* Catalogs: ``openalloy/locales/<lang>/LC_MESSAGES/openalloy.po`` (the source of
  truth, edited by translators) compiled to ``.mo`` by ``tools/i18n.py``.
* Languages: ``en`` (default and fallback) and ``pt_BR``. The choice is stored in
  the settings (``language``) and shared by the service, the CLI and the window.
  The system locale does *not* change the default: the first run is in English.
* Regional formatting (numbers, percentages, dates) lives here too, so no
  regional format is hard-coded elsewhere.

``python3 -m openalloy.i18n tr MSG`` translates one message (used by the shell
installer, which runs before the package is installed).
"""

from __future__ import annotations

import gettext as _gettext
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

DOMAIN = "openalloy"
LOCALEDIR = Path(__file__).resolve().parent.parent / "locales"
DEFAULT = "en"
# code -> name shown in its own language (never translated)
LANGUAGES: Dict[str, str] = {"en": "English", "pt_BR": "Português (Brasil)"}
ENV = "OPENALLOY_LANG"

_lock = threading.RLock()
_current = DEFAULT
_trans: _gettext.NullTranslations = _gettext.NullTranslations()
_listeners: List[Callable[[str], None]] = []


# --- language selection ----------------------------------------------------------------

def normalize(code: Optional[str]) -> Optional[str]:
    """'pt-BR', 'pt_br', 'pt_BR.UTF-8', 'pt' -> 'pt_BR'; 'en_US' -> 'en'; unknown -> None."""
    if not code or not isinstance(code, str):
        return None
    c = code.strip().split(".")[0].split("@")[0].replace("-", "_")
    if not c:
        return None
    for known in LANGUAGES:
        if c.lower() == known.lower():
            return known
    base = c.split("_")[0].lower()
    return {"pt": "pt_BR", "en": "en"}.get(base)


def set_language(code: Optional[str]) -> str:
    """Switch the process language; invalid or unavailable codes fall back to English."""
    global _current, _trans
    lang = normalize(code) or DEFAULT
    trans: _gettext.NullTranslations
    if lang == DEFAULT:
        trans = _gettext.NullTranslations()
    else:
        try:
            trans = _gettext.translation(DOMAIN, str(LOCALEDIR), languages=[lang])
        except OSError:                      # catalog missing: English, never raw keys
            trans, lang = _gettext.NullTranslations(), DEFAULT
    with _lock:
        changed = lang != _current
        _current, _trans = lang, trans
        listeners = list(_listeners)
    if changed:
        for fn in listeners:
            try:
                fn(lang)
            except Exception:                # a broken listener must not block the switch
                pass
    return lang


def current() -> str:
    return _current


def on_change(fn: Callable[[str], None]) -> None:
    with _lock:
        _listeners.append(fn)


def resolve(explicit: Optional[str] = None, settings_path: Optional[Path] = None) -> str:
    """Language to use: explicit (--lang) > $OPENALLOY_LANG > saved preference > English."""
    for cand in (explicit, os.environ.get(ENV)):
        lang = normalize(cand)
        if lang:
            return lang
    path = settings_path or _config_file()
    try:
        with open(path, "rb") as fh:
            raw = fh.read(1024 * 1024 + 1)          # same bound as the settings loader
        saved = None if len(raw) > 1024 * 1024 else json.loads(raw.decode("utf-8")).get("language")
    except (OSError, ValueError, AttributeError, RecursionError):
        saved = None                                # a broken file never stops the program
    return normalize(saved) or DEFAULT


def _config_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / "openalloy" / "config.json"


# --- translation functions ---------------------------------------------------------------

def _(message: str) -> str:
    return _trans.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _trans.ngettext(singular, plural, n)


def pgettext(context: str, message: str) -> str:
    return _trans.pgettext(context, message)


def npgettext(context: str, singular: str, plural: str, n: int) -> str:
    return _trans.npgettext(context, singular, plural, n)


def N_(message: str) -> str:          # noqa: N802 - gettext convention
    """Mark for extraction only; translate later with _()."""
    return message


class Message:
    """A message translated when it is *shown*, not when it is created.

    Exceptions raised by the core carry one of these instead of a ready-made string:
    ``str()`` gives the text in the current language (window, CLI) and ``english()``
    the original, for the service log, which stays in one language (docs/DEVELOPMENT.md).
    Mark the msgid with ``N_`` so it is extracted: ``Message(N_("{path} gone"), path=p)``.

    Exceptions given as parameters are reduced to their message (or text) at once, so a
    stored Message never keeps a traceback - and the objects it references - alive.
    A tuple parameter is a list of reasons, shown as "a; b".
    """

    __slots__ = ("msgid", "params")

    def __init__(self, msgid: str, **params):
        self.msgid = msgid
        self.params = {k: _detach(v) for k, v in params.items()}

    def _render(self, template: str, conv: Callable[[object], str]) -> str:
        # only nested messages are rendered here; other values keep their type so
        # "{value!r}" and number formats behave as with str.format
        def one(v):
            if isinstance(v, Message):
                return conv(v)
            if isinstance(v, tuple):
                return "; ".join(one(x) if isinstance(x, Message) else str(x) for x in v)
            return v
        values = {k: one(v) for k, v in self.params.items()}
        try:
            return template.format(**values)
        except (KeyError, IndexError, ValueError):     # a broken translation: original
            return self.msgid.format(**values)

    def __str__(self) -> str:
        return self._render(_(self.msgid), str)

    def english(self) -> str:
        return self._render(self.msgid, english)

    def __repr__(self) -> str:
        return f"Message({self.msgid!r}, {self.params!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Message) and (self.msgid, self.english()) == (
            other.msgid, other.english())

    def __hash__(self) -> int:
        return hash((self.msgid, self.english()))


def message_of(obj: object):
    """The Message an exception carries (with its errno), or None."""
    if isinstance(obj, Message):
        return obj
    if isinstance(obj, OSError) and isinstance(obj.strerror, Message) and obj.filename is None:
        return Message("[Errno {errno}] {text}", errno=obj.errno, text=obj.strerror)
    if isinstance(obj, BaseException) and len(obj.args) == 1 and isinstance(obj.args[0], Message):
        return obj.args[0]
    return None


def _detach(value):
    if isinstance(value, BaseException):
        msg = message_of(value)
        return msg if msg is not None else str(value)
    if isinstance(value, (tuple, list)):
        return tuple(_detach(v) for v in value)
    return value


def english(obj: object) -> str:
    """Text of a Message, an exception carrying one, or anything else, in English."""
    msg = message_of(obj)
    return msg.english() if msg is not None else str(obj)


def install_argparse() -> None:
    """argparse calls gettext.gettext from its own module; route it to our catalog so
    usage, errors and built-in help are translated too."""
    import argparse
    argparse._ = _                    # type: ignore[attr-defined]
    argparse.ngettext = ngettext      # type: ignore[attr-defined]


# --- regional formatting --------------------------------------------------------------------
# One table per language: decimal and thousands separators, percent spacing and
# date patterns (strftime with English-independent tokens only).
_REGION = {
    "en": {"dec": ".", "thou": ",", "pct": "{}%", "date": "%Y-%m-%d",
           "datetime": "%Y-%m-%d %H:%M"},
    "pt_BR": {"dec": ",", "thou": ".", "pct": "{}%", "date": "%d/%m/%Y",
              "datetime": "%d/%m/%Y %H:%M"},
}


def _region() -> dict:
    return _REGION.get(_current, _REGION[DEFAULT])


def format_number(value: float, decimals: int = 0) -> str:
    r = _region()
    text = f"{float(value):,.{max(0, int(decimals))}f}"
    return text.replace(",", "\x00").replace(".", r["dec"]).replace("\x00", r["thou"])


def format_percent(fraction: float, decimals: int = 0) -> str:
    """0.5 -> '50%' (en) / '50%' (pt_BR); 0.255 with 1 decimal -> '25.5%' / '25,5%'."""
    return _region()["pct"].format(format_number(float(fraction) * 100, decimals))


def format_datetime(ts: Optional[float] = None, with_time: bool = True) -> str:
    t = time.localtime(time.time() if ts is None else ts)
    return time.strftime(_region()["datetime" if with_time else "date"], t)


def parse_datetime(text: str) -> Optional[float]:
    """Read back a stored timestamp written in ISO form (storage is always ISO)."""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except (TypeError, ValueError):
            continue
    return None
