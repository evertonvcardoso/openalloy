#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Translation catalog maintenance for OpenAlloy (pure Python, no GNU gettext needed).

    python3 tools/i18n.py extract   # source -> openalloy/locales/openalloy.pot
    python3 tools/i18n.py update    # .pot -> every .po (keeps translations, marks obsolete)
    python3 tools/i18n.py compile   # .po -> .mo  (+ openalloy.desktop from its template)
    python3 tools/i18n.py check     # syntax, missing/fuzzy, placeholders, sync, stale .mo
    python3 tools/i18n.py stats     # coverage per language
    python3 tools/i18n.py all       # extract + update + compile + check

New language: create ``openalloy/locales/<code>/LC_MESSAGES/``, copy the .pot there
as ``openalloy.po``, set ``Language`` and ``Plural-Forms`` in the header, translate,
run ``all`` and add the code to ``openalloy.i18n.LANGUAGES``.

What is extracted: string literals passed to ``_``, ``N_``, ``ngettext``,
``pgettext``, ``npgettext`` in Python files (a non-literal argument is an error),
``t "..."`` calls in the shell scripts, and Name/GenericName/Comment/Keywords of
``openalloy.desktop.in`` (context ``desktop``).
"""

from __future__ import annotations

import ast
import re
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "openalloy"
LOCALES = PKG / "locales"
POT = LOCALES / "openalloy.pot"
DOMAIN = "openalloy"
DESKTOP_IN = PKG / "resources" / "openalloy.desktop.in"
DESKTOP_OUT = PKG / "resources" / "openalloy.desktop"
DESKTOP_KEYS = ("Name", "GenericName", "Comment", "Keywords")
PY_SOURCES = [PKG, ROOT / "tools" / "hid_diagnostics.py"]
SH_SOURCES = [ROOT / "install.sh", ROOT / "uninstall.sh"]
PLURAL_FORMS = {"en": "nplurals=2; plural=(n != 1);", "pt_BR": "nplurals=2; plural=(n > 1);"}
FUNCS = {"_": ("msgid",), "N_": ("msgid",), "ngettext": ("msgid", "plural"),
         "pgettext_noop": ("ctx", "msgid"),
         "pgettext": ("ctx", "msgid"), "npgettext": ("ctx", "msgid", "plural")}
DYNAMIC: List[str] = []          # _(variable) call sites (strings marked with N_ elsewhere)
PLACEHOLDER = re.compile(r"%\([A-Za-z_]\w*\)[-#0 +]*\d*(?:\.\d+)?[sdifr]|%[-#0 +]*\d*(?:\.\d+)?[sdifrxX]"
                         r"|\{[A-Za-z_]\w*(?:![rsa])?(?::[^{}]*)?\}")


# --- catalog model --------------------------------------------------------------------------

@dataclass
class Entry:
    msgid: str
    ctx: Optional[str] = None
    plural: Optional[str] = None
    msgstr: List[str] = field(default_factory=lambda: [""])
    refs: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    comments: List[str] = field(default_factory=list)
    obsolete: bool = False

    @property
    def key(self) -> Tuple[Optional[str], str]:
        return (self.ctx, self.msgid)

    @property
    def translated(self) -> bool:
        return all(self.msgstr) and "fuzzy" not in self.flags


def _escape(s: str) -> str:
    return (s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            .replace("\t", "\\t"))


def _unescape(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(n, n))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _po_string(keyword: str, value: str) -> str:
    if "\n" in value[:-1]:
        parts = value.split("\n")
        lines = [p + "\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
        return f'{keyword} ""\n' + "\n".join(f'"{_escape(x)}"' for x in lines)
    return f'{keyword} "{_escape(value)}"'


class PoError(Exception):
    pass


def parse_po(path: Path) -> Tuple[str, List[Entry]]:
    """Return (header, entries). Raises PoError with line numbers on bad syntax."""
    text = path.read_bytes().decode("utf-8")      # UnicodeDecodeError = not UTF-8
    header = ""
    entries: List[Entry] = []
    cur: Dict = {}
    last: Optional[str] = None

    def flush():
        nonlocal cur, last, header
        if "msgid" in cur:
            e = Entry(cur["msgid"], cur.get("ctx"), cur.get("plural"),
                      [cur["msgstr"].get(i, "") for i in range(max(cur["msgstr"]) + 1)]
                      if cur.get("msgstr") else [""],
                      cur.get("refs", []), cur.get("flags", []), cur.get("comments", []),
                      cur.get("obsolete", False))
            if e.msgid == "" and e.ctx is None and not e.obsolete:
                header = e.msgstr[0]
            else:
                entries.append(e)
        elif cur.get("refs") or cur.get("flags"):
            raise PoError(f"{path}: comment without an entry")
        cur, last = {}, None

    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        obsolete = line.startswith("#~")
        if obsolete:
            line = line[2:].strip()
            cur["obsolete"] = True
        if not line:
            flush()
            continue
        if line.startswith("#:"):
            if "msgid" in cur:
                flush()
            cur.setdefault("refs", []).extend(line[2:].split())
            continue
        if line.startswith("#,"):
            if "msgid" in cur:
                flush()
            cur.setdefault("flags", []).extend(f.strip() for f in line[2:].split(","))
            continue
        if line.startswith("#"):
            if "msgid" in cur:
                flush()
            cur.setdefault("comments", []).append(line)
            continue
        m = re.match(r'^(msgctxt|msgid_plural|msgid|msgstr(?:\[(\d+)\])?)\s+"(.*)"$', line)
        if m:
            kw, idx, val = m.group(1), m.group(2), _unescape(m.group(3))
            if kw == "msgctxt":
                if "msgid" in cur:
                    flush()
                    cur["obsolete"] = obsolete
                cur["ctx"], last = val, "ctx"
            elif kw == "msgid":
                if "msgid" in cur:
                    flush()
                    cur["obsolete"] = obsolete
                cur["msgid"], last = val, "msgid"
            elif kw == "msgid_plural":
                cur["plural"], last = val, "plural"
            else:
                n = int(idx) if idx is not None else 0
                cur.setdefault("msgstr", {})[n] = val
                last = ("msgstr", n)
            continue
        m = re.match(r'^"(.*)"$', line)
        if m and last is not None:
            val = _unescape(m.group(1))
            if isinstance(last, tuple):
                cur["msgstr"][last[1]] += val
            else:
                cur[last] += val
            continue
        raise PoError(f"{path}:{no}: invalid line: {raw!r}")
    flush()
    return header, entries


LANGUAGE_NAMES = {None: "Message template", "en": "English (source language) messages",
                  "pt_BR": "Brazilian Portuguese translation"}


def preamble(lang: Optional[str]) -> List[str]:
    """GNU-style comment block at the top of a .po/.pot file."""
    what = LANGUAGE_NAMES.get(lang, f"{lang} translation")
    return [f"# {what} for OpenAlloy.",
            "# Copyright (C) 2026 Everton Vilhena Cardoso",
            "# This file is distributed under the same license as the openalloy package.",
            "# Everton Vilhena Cardoso, 2026.",
            "#"]


def _version() -> str:
    m = re.search(r'__version__ = "([^"]+)"', (PKG / "__init__.py").read_text(encoding="utf-8"))
    return m.group(1) if m else "0"


def write_po(path: Path, header: str, entries: List[Entry]) -> None:
    lang = None if path.suffix == ".pot" else path.parent.parent.name
    out = preamble(lang) + ['msgid ""', _po_string("msgstr", header), ""]
    for e in entries:
        pre = "#~ " if e.obsolete else ""
        out += [c for c in e.comments]
        if e.refs and not e.obsolete:
            refs = " ".join(e.refs)
            out.append("#: " + refs)
        if e.flags:
            out.append("#, " + ", ".join(dict.fromkeys(e.flags)))
        lines = []
        if e.ctx is not None:
            lines.append(_po_string("msgctxt", e.ctx))
        lines.append(_po_string("msgid", e.msgid))
        if e.plural is not None:
            lines.append(_po_string("msgid_plural", e.plural))
            for i, s in enumerate(e.msgstr):
                lines.append(_po_string(f"msgstr[{i}]", s))
        else:
            lines.append(_po_string("msgstr", e.msgstr[0] if e.msgstr else ""))
        for block in lines:
            out += [pre + x for x in block.split("\n")]
        out.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")


def header_for(lang: Optional[str]) -> str:
    now = time.strftime("%Y-%m-%d %H:%M+0000", time.gmtime())
    lines = [f"Project-Id-Version: {DOMAIN} {_version()}", f"POT-Creation-Date: {now}"]
    if lang:
        lines += ["Last-Translator: Everton Vilhena Cardoso",
                  "Language-Team: Everton Vilhena Cardoso"]
    lines += ["MIME-Version: 1.0", "Content-Type: text/plain; charset=UTF-8",
              "Content-Transfer-Encoding: 8bit"]
    if lang:
        lines += [f"Language: {lang}", f"Plural-Forms: {PLURAL_FORMS.get(lang, PLURAL_FORMS['en'])}"]
    return "\n".join(lines) + "\n"


# --- extraction --------------------------------------------------------------------------------

def _py_files() -> List[Path]:
    files: List[Path] = []
    for src in PY_SOURCES:
        files += sorted(src.rglob("*.py")) if src.is_dir() else [src]
    return [f for f in files if "locales" not in f.parts]


def extract() -> List[Entry]:
    found: Dict[Tuple[Optional[str], str], Entry] = {}
    errors: List[str] = []

    def add(ctx, msgid, plural, ref, flags=()):
        e = found.get((ctx, msgid))
        if e is None:
            e = found[(ctx, msgid)] = Entry(msgid, ctx, plural,
                                            ["", ""] if plural is not None else [""])
        elif plural is not None and e.plural is not None and plural != e.plural:
            errors.append(f"{ref}: different plural for {msgid!r}")
        e.refs.append(ref)
        if PLACEHOLDER.search(msgid) or (plural and PLACEHOLDER.search(plural)):
            if "%" in msgid + (plural or ""):
                e.flags.append("python-format")
            if "{" in msgid + (plural or ""):
                e.flags.append("python-brace-format")
        e.flags.extend(flags)

    for f in _py_files():
        rel = f.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"), str(f))
        except SyntaxError as exc:
            errors.append(f"{rel}: {exc}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
            if name not in FUNCS or (isinstance(fn, ast.Attribute) and name == "_"):
                continue
            roles = FUNCS[name]
            args = node.args[:len(roles)]
            if len(args) < len(roles):
                continue
            vals = {}
            ok = True
            for role, arg in zip(roles, args):
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    vals[role] = arg.value
                else:
                    ok = False
            if not ok:
                # _(variable) translates a string marked elsewhere with N_(): fine.
                DYNAMIC.append(f"{rel}:{node.lineno}")
                continue
            if vals["msgid"] == "":
                errors.append(f"{rel}:{node.lineno}: empty msgid")
                continue
            add(vals.get("ctx"), vals["msgid"], vals.get("plural"), f"{rel}:{node.lineno}")

    shell = re.compile(r'(?<![\w$])t\s+"((?:[^"\\]|\\.)*)"')
    for f in SH_SOURCES:
        rel = f.relative_to(ROOT).as_posix()
        for no, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for m in shell.finditer(line):
                msg = m.group(1).replace('\\"', '"').replace("\\$", "$").replace("\\\\", "\\")
                add(None, msg, None, f"{rel}:{no}", ("sh-format",) if "%s" in msg else ())

    import json
    for f in sorted((PKG / "resources" / "presets").glob("*.json")):     # built-in preset names
        rel = f.relative_to(ROOT).as_posix()
        name = json.loads(f.read_text(encoding="utf-8")).get("name")
        if isinstance(name, str) and name:
            add("preset", name, None, rel)

    for no, line in enumerate(DESKTOP_IN.read_text(encoding="utf-8").splitlines(), 1):
        k, _, v = line.partition("=")
        if k in DESKTOP_KEYS and v:
            add("desktop", v, None, f"{DESKTOP_IN.relative_to(ROOT).as_posix()}:{no}")

    if errors:
        raise SystemExit("extraction errors:\n  " + "\n  ".join(errors))
    for e in found.values():
        e.flags = list(dict.fromkeys(e.flags))
    return list(found.values())


def cmd_extract() -> None:
    entries = extract()
    write_po(POT, header_for(None), entries)
    print(f"{POT.relative_to(ROOT)}: {len(entries)} messages")


# --- update / compile / check ------------------------------------------------------------------------

def languages() -> List[str]:
    return sorted(p.parent.name for p in LOCALES.glob("*/LC_MESSAGES"))


def po_path(lang: str) -> Path:
    return LOCALES / lang / "LC_MESSAGES" / f"{DOMAIN}.po"


def mo_path(lang: str) -> Path:
    return po_path(lang).with_suffix(".mo")


def cmd_update() -> None:
    _, template = parse_po(POT)
    for lang in languages():
        path = po_path(lang)
        old: Dict = {}
        if path.exists():
            _hdr, entries = parse_po(path)
            old = {e.key: e for e in entries}
        merged: List[Entry] = []
        for t in template:
            prev = old.pop(t.key, None)
            e = Entry(t.msgid, t.ctx, t.plural, list(t.msgstr), list(t.refs), list(t.flags))
            if prev is not None and (prev.plural is None) == (t.plural is None):
                e.msgstr = prev.msgstr if len(prev.msgstr) == len(t.msgstr) else e.msgstr
                if "fuzzy" in prev.flags:
                    e.flags.append("fuzzy")
                e.comments = [c for c in prev.comments if c.startswith("# ")]
            if lang == "en":                       # source language: identity catalog
                e.msgstr = [t.msgid] if t.plural is None else [t.msgid, t.plural]
            merged.append(e)
        obsolete = [e for e in old.values() if e.msgstr and any(e.msgstr)]
        for e in obsolete:
            e.obsolete, e.refs = True, []
        write_po(path, header_for(lang), merged + obsolete)
        print(f"{path.relative_to(ROOT)}: {len(merged)} messages, {len(obsolete)} obsolete")


def _placeholders(s: str) -> List[str]:
    return sorted(PLACEHOLDER.findall(s))


def placeholders_match(e: Entry, s: str) -> bool:
    """A translation must use exactly the variables of its source (any plural form)."""
    src = [e.msgid] + ([e.plural] if e.plural else [])
    return any(_placeholders(s) == _placeholders(x) for x in src) or bool(
        e.plural and set(_placeholders(s)) <= set(_placeholders(e.plural)))


def build_mo(header: str, entries: List[Entry]) -> bytes:
    """GNU .mo (little-endian, no hash table). Untranslated/fuzzy entries are left out,
    so gettext falls back to the English source text."""
    pairs = {"": header}
    for e in entries:
        if e.obsolete or not e.translated:
            continue
        if not all(placeholders_match(e, s) for s in e.msgstr):
            continue        # a broken translation would crash .format(): use English
        k = e.msgid if e.plural is None else e.msgid + "\x00" + e.plural
        if e.ctx is not None:
            k = e.ctx + "\x04" + k
        pairs[k] = "\x00".join(e.msgstr)
    keys = sorted(pairs, key=lambda k: k.encode("utf-8"))
    ids = [k.encode("utf-8") for k in keys]
    strs = [pairs[k].encode("utf-8") for k in keys]
    n = len(keys)
    k_off = 28
    v_off = k_off + n * 8
    data_off = v_off + n * 8
    table_k, table_v, blob = [], [], b""
    for b in ids:
        table_k.append((len(b), data_off + len(blob)))
        blob += b + b"\x00"
    for b in strs:
        table_v.append((len(b), data_off + len(blob)))
        blob += b + b"\x00"
    out = struct.pack("<7I", 0x950412DE, 0, n, k_off, v_off, 0, 0)
    out += b"".join(struct.pack("<2I", *x) for x in table_k)
    out += b"".join(struct.pack("<2I", *x) for x in table_v)
    return out + blob


def render_desktop() -> str:
    tr: Dict[str, Dict[str, str]] = {}
    for lang in languages():
        if lang == "en":
            continue
        _h, entries = parse_po(po_path(lang))
        tr[lang] = {e.msgid: e.msgstr[0] for e in entries
                    if e.ctx == "desktop" and e.translated and not e.obsolete}
    out = []
    for line in DESKTOP_IN.read_text(encoding="utf-8").splitlines():
        out.append(line)
        k, _, v = line.partition("=")
        if k in DESKTOP_KEYS and v:
            for lang, table in tr.items():
                if v in table and table[v] != v:
                    out.append(f"{k}[{lang}]={table[v]}")
    return "# Generated by tools/i18n.py from openalloy.desktop.in - do not edit\n" + \
        "\n".join(out) + "\n"


def cmd_compile() -> None:
    for lang in languages():
        header, entries = parse_po(po_path(lang))
        mo_path(lang).write_bytes(build_mo(header, entries))
        print(f"{mo_path(lang).relative_to(ROOT)} compiled")
    DESKTOP_OUT.write_text(render_desktop(), encoding="utf-8")
    print(f"{DESKTOP_OUT.relative_to(ROOT)} generated")




def check() -> List[str]:
    problems: List[str] = []
    try:
        _h, template = parse_po(POT)
    except (OSError, PoError, UnicodeDecodeError) as exc:
        return [f"POT: {exc}"]
    current = {e.key for e in extract()}
    pot_keys = {e.key for e in template}
    for k in sorted(current - pot_keys, key=str):
        problems.append(f"POT out of date: missing {k[1]!r} (run extract)")
    for k in sorted(pot_keys - current, key=str):
        problems.append(f"POT out of date: stale {k[1]!r} (run extract)")
    for lang in languages():
        path = po_path(lang)
        try:
            header, entries = parse_po(path)
        except (OSError, PoError, UnicodeDecodeError) as exc:
            problems.append(f"{lang}: {exc}")
            continue
        if "charset=UTF-8" not in header:
            problems.append(f"{lang}: header without charset=UTF-8")
        if "Plural-Forms:" not in header:
            problems.append(f"{lang}: header without Plural-Forms")
        seen = set()
        live = [e for e in entries if not e.obsolete]
        for e in live:
            where = f"{lang}: {e.msgid[:50]!r}"
            if e.key in seen:
                problems.append(f"{where}: duplicate entry")
            seen.add(e.key)
            if not all(e.msgstr):
                problems.append(f"{where}: untranslated")
            if "fuzzy" in e.flags:
                problems.append(f"{where}: marked fuzzy")
            for s in e.msgstr:
                if s and not placeholders_match(e, s):
                    problems.append(f"{where}: placeholders differ ({_placeholders(s)} vs "
                                    f"{_placeholders(e.msgid)})")
            if e.plural is not None and len(e.msgstr) != 2:
                problems.append(f"{where}: plural needs 2 forms")
        keys = {e.key for e in live}
        for k in sorted(pot_keys - keys, key=str):
            problems.append(f"{lang}: missing {k[1]!r} (run update)")
        for k in sorted(keys - pot_keys, key=str):
            problems.append(f"{lang}: unused entry {k[1]!r} (run update)")
        if not mo_path(lang).exists() or mo_path(lang).read_bytes() != build_mo(header, entries):
            problems.append(f"{lang}: .mo out of date (run compile)")
    if DESKTOP_OUT.read_text(encoding="utf-8") != render_desktop():
        problems.append("openalloy.desktop out of date (run compile)")
    return problems


def stats() -> Dict[str, Tuple[int, int]]:
    out = {}
    for lang in languages():
        _h, entries = parse_po(po_path(lang))
        live = [e for e in entries if not e.obsolete]
        out[lang] = (sum(e.translated for e in live), len(live))
    return out


def main(argv: List[str]) -> int:
    cmd = argv[0] if argv else "check"
    if cmd == "extract":
        cmd_extract()
    elif cmd == "update":
        cmd_update()
    elif cmd == "compile":
        cmd_compile()
    elif cmd == "stats":
        for lang, (done, total) in stats().items():
            print(f"{lang:6} {done}/{total} ({100 * done / max(total, 1):.1f}%)")
    elif cmd == "all":
        cmd_extract()
        cmd_update()
        cmd_compile()
        return main(["check"])
    elif cmd == "check":
        problems = check()
        for p in problems:
            print("  -", p)
        print("catalogs OK" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
