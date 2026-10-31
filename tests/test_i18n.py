# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Internationalisation: English default, pt_BR, fallback, persistence, plurals,
regional formats, catalog integrity and no hard-coded user-facing text."""
import ast
import json
import re
import sys
import time
from pathlib import Path

import pytest

from openalloy import i18n
from openalloy.i18n import _, format_datetime, format_number, format_percent, ngettext, pgettext

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import i18n as catalog_tool  # noqa: E402  (tools/i18n.py)

# --- language selection --------------------------------------------------------------------

def test_starts_in_english_even_on_a_portuguese_system(monkeypatch):
    monkeypatch.setenv("LANG", "pt_BR.UTF-8")
    monkeypatch.setenv("LANGUAGE", "pt_BR")
    assert i18n.resolve() == "en"
    assert i18n.set_language(i18n.resolve()) == "en"
    assert _("Apply") == "Apply"


def test_portuguese_can_be_selected():
    i18n.set_language("pt_BR")
    assert _("Apply") == "Aplicar"
    assert _("Fix on keyboard") == "Fixar no teclado"


@pytest.mark.parametrize("code,expected", [("pt-BR", "pt_BR"), ("pt_br", "pt_BR"),
                                           ("pt_BR.UTF-8", "pt_BR"), ("pt", "pt_BR"),
                                           ("en_US", "en"), ("EN", "en"), ("de", None),
                                           ("", None), (None, None), (5, None)])
def test_locale_codes_are_normalised(code, expected):
    assert i18n.normalize(code) == expected


@pytest.mark.parametrize("code", ["de", "xx_YY", "", None])
def test_invalid_language_falls_back_to_english(code):
    i18n.set_language("pt_BR")
    assert i18n.set_language(code) == "en"
    assert _("Apply") == "Apply"


def test_missing_catalog_falls_back_to_english(monkeypatch, tmp_path):
    monkeypatch.setattr(i18n, "LOCALEDIR", tmp_path)          # no .mo anywhere
    assert i18n.set_language("pt_BR") == "en"
    assert _("Apply") == "Apply"


def test_untranslated_message_shows_english_not_a_key():
    i18n.set_language("pt_BR")
    assert _("A message nobody translated") == "A message nobody translated"


def test_preference_is_saved_and_respected(tmp_path):
    from openalloy.config.settings import Settings
    st = Settings(tmp_path / "c.json")
    assert st.get("language") == "en"
    st.update(language="pt-BR")
    assert st.get("language") == "pt_BR"
    assert i18n.resolve(settings_path=tmp_path / "c.json") == "pt_BR"
    with pytest.raises(ValueError):
        st.update(language="klingon")


def test_explicit_and_environment_override_the_saved_preference(monkeypatch, tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"language": "pt_BR"}))
    assert i18n.resolve(settings_path=cfg) == "pt_BR"
    monkeypatch.setenv("OPENALLOY_LANG", "en")
    assert i18n.resolve(settings_path=cfg) == "en"
    assert i18n.resolve("pt", settings_path=cfg) == "pt_BR"


def test_listeners_hear_language_changes():
    seen = []
    i18n.on_change(seen.append)
    i18n.set_language("pt_BR")
    i18n.set_language("pt_BR")          # no change, no event
    i18n.set_language("en")
    assert seen[-2:] == ["pt_BR", "en"]


# --- plurals, context, interpolation ---------------------------------------------------------

@pytest.mark.parametrize("lang,n,expected", [
    ("en", 0, "0 keys selected"), ("en", 1, "1 key selected"), ("en", 2, "2 keys selected"),
    ("pt_BR", 0, "0 tecla selecionada"), ("pt_BR", 1, "1 tecla selecionada"),
    ("pt_BR", 2, "2 teclas selecionadas")])
def test_plural_rules_of_each_language(lang, n, expected):
    i18n.set_language(lang)
    assert ngettext("{n} key selected", "{n} keys selected", n).format(n=n) == expected


def test_context_separates_identical_words():
    i18n.set_language("pt_BR")
    assert pgettext("keys", "None") == "Nenhuma"        # nenhuma tecla
    assert pgettext("preset", "None") == "Nenhum"       # nenhum preset


def test_variables_are_interpolated_in_both_languages():
    for lang, word in (("en", "Profile"), ("pt_BR", "Perfil")):
        i18n.set_language(lang)
        text = _("Profile {profile} active on the keyboard.").format(profile=3)
        assert text.startswith(word) and "3" in text


def test_every_translation_accepts_the_same_variables():
    """Formatting each pt_BR string with the English variables must never fail."""
    _h, entries = catalog_tool.parse_po(catalog_tool.po_path("pt_BR"))
    for e in entries:
        for src, dst in zip([e.msgid] + ([e.plural] if e.plural else []), e.msgstr * 2):
            names = set(re.findall(r"\{(\w+)[^{}]*\}", src))
            if names and "python-brace-format" in e.flags:
                values = {k: 1 for k in names}
                dst.format(**values)                     # KeyError/ValueError = bug
            pct = re.findall(r"%\((\w+)\)", src)
            if pct and "python-format" in e.flags:
                dst % {k: "x" for k in pct}


# --- regional formatting ----------------------------------------------------------------------

def test_numbers_follow_the_language():
    i18n.set_language("en")
    assert format_number(1234.5, 1) == "1,234.5" and format_percent(0.255, 1) == "25.5%"
    i18n.set_language("pt_BR")
    assert format_number(1234.5, 1) == "1.234,5" and format_percent(0.255, 1) == "25,5%"
    assert format_percent(0.5) == "50%"


def test_dates_follow_the_language():
    ts = time.mktime((2026, 10, 30, 14, 5, 0, 0, 0, -1))
    i18n.set_language("en")
    assert format_datetime(ts) == "2026-10-30 14:05"
    i18n.set_language("pt_BR")
    assert format_datetime(ts) == "30/10/2026 14:05"
    assert format_datetime(ts, with_time=False) == "30/10/2026"


def test_flash_gate_date_is_shown_in_the_regional_format():
    from openalloy.application import flashgate
    info = {"usb_id": "03f0:098f", "serial": "x", "driver": "d", "firmware": "21.08"}
    key, _rec = flashgate.record(info)
    validated = {key: {"firmware": "21.08", "date": "2026-10-30 14:05"}}
    i18n.set_language("pt_BR")
    assert flashgate.check(info, validated)[1] == "RGB ao vivo confirmado em 30/10/2026 14:05"


# --- catalogs ----------------------------------------------------------------------------------

def test_catalogs_are_complete_and_in_sync():
    """Same checks as `tools/i18n.py check`: POT matches the code, no missing, fuzzy,
    duplicate or unused entries, placeholders match, .mo and .desktop are up to date."""
    assert catalog_tool.check() == []


def test_full_coverage_in_both_languages():
    for lang, (done, total) in catalog_tool.stats().items():
        assert done == total, f"{lang}: {done}/{total}"


def test_effect_texts_are_all_in_the_catalog():
    from openalloy.application.effects import describe_effects, describe_reaction
    i18n.set_language("pt_BR")
    texts = []
    for e in describe_effects():
        texts += [e["label"], e["description"]]
        for p in e["params"]:
            texts.append(p["label"])
            texts += [p["help"]] if p["help"] else []
            texts += list((p["choices"] or {}).values())
    for p in describe_reaction():
        texts.append(p["label"])
        texts += list((p["choices"] or {}).values())
    same = {"Scanner", "Vertical"}                     # identical in Portuguese
    untranslated = [t for t in texts if _(t) == t and t not in same]
    assert untranslated == []


def test_builtin_preset_names_are_translated():
    from openalloy.config.settings import PresetStore
    i18n.set_language("pt_BR")
    shown = {pgettext("preset", p["name"]) for p in PresetStore().list() if p["builtin"]}
    assert "Pôr do sol" in shown and "Oceano" in shown
    assert PresetStore().get("Pôr do sol")["id"] == "por-do-sol"   # lookup by the shown name


def test_desktop_entry_has_portuguese_keys():
    text = (ROOT / "openalloy" / "resources" / "openalloy.desktop").read_text(encoding="utf-8")
    assert "GenericName=HyperX keyboard configurator" in text
    assert "GenericName[pt_BR]=Configurador de teclado HyperX" in text


def test_shell_installer_translates(tmp_path):
    import subprocess
    out = subprocess.run(["bash", str(ROOT / "install.sh"), "--lang", "pt_BR", "--help"],
                         capture_output=True, text=True, timeout=60).stdout
    assert out.startswith("Uso: ./install.sh [opções]")
    out = subprocess.run(["bash", str(ROOT / "install.sh"), "--help"],
                         capture_output=True, text=True, timeout=60).stdout
    assert out.startswith("Usage: ./install.sh [options]")


# --- no hard-coded user-facing text ------------------------------------------------------------

# Calls whose string argument is shown to the user.
UI_SINKS = {"Label", "Button", "ToggleButton", "MenuButton", "Entry", "Expander", "set_text",
            "set_label", "set_tooltip_text", "set_message", "set_detail", "set_title",
            "add_titled", "labeled", "dim", "frame", "expander", "print", "error", "row",
            "line", "SystemExit", "EngineError", "ProtocolError", "RpcError",
            "LampArrayError", "TransportError", "DeviceNotFound", "PermissionDenied",
            "new_from_strings", "set_placeholder_text", "add_argument", "add_parser",
            "ArgumentParser", "ask_yes", "report", "tick"}
UI_KEYWORDS = {"label", "help", "description", "tooltip_text", "placeholder_text", "title"}
# literals that are not language: brand/technical names, symbols, formats
ALLOWED = re.compile(r"^(|OpenAlloy|LampArray|HyperX.*|[\W\d_]*|[a-z0-9_.:/-]+|%[-#0 +]*\d*\w|"
                     r"OK|USB|JSON|RGB|v\{.*\}|Python .*|\{.*\})$")


def _ui_string_literals(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
        candidates = []
        if name in UI_SINKS:
            candidates += node.args[:1] if name not in ("add_titled", "add_parser") else node.args[1:3]
        candidates += [k.value for k in node.keywords if k.arg in UI_KEYWORDS]
        for arg in candidates:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Call):      # _(...), N_(...) etc. handle their own text
                    break
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    text = sub.value.strip()
                    if re.search(r"[A-Za-z]{3,}", text) and not ALLOWED.match(text):
                        yield sub.lineno, text
                if isinstance(sub, ast.JoinedStr):
                    for v in sub.values:
                        if isinstance(v, ast.Constant) and re.search(r"[A-Za-z]{3,}", v.value) \
                                and not ALLOWED.match(v.value.strip()):
                            yield sub.lineno, v.value.strip()


@pytest.mark.parametrize("rel", ["openalloy/presentation/cli/main.py",
                                 "openalloy/presentation/gui/app.py",
                                 "openalloy/presentation/gui/widgets.py",
                                 "openalloy/presentation/daemon/main.py"])
def test_no_hard_coded_text_reaches_the_user(rel):
    found = [f"{rel}:{ln}: {txt!r}" for ln, txt in _ui_string_literals(ROOT / rel)]
    assert found == []


# --- end to end ---------------------------------------------------------------------------------

def test_cli_output_and_help_follow_the_language(capsys, monkeypatch):
    from openalloy.presentation.cli.main import main
    assert main(["--simulate-hp", "--lang", "pt_BR", "effects"]) == 0
    out = capsys.readouterr().out
    assert "Onda arco-íris" in out and "somente ao vivo" in out
    with pytest.raises(SystemExit):
        main(["--lang", "pt_BR", "--help"])
    out = capsys.readouterr().out
    assert out.startswith("uso: openalloy") and "mostra esta ajuda e sai" in out
    monkeypatch.delenv("OPENALLOY_LANG", raising=False)   # --lang exports it for child processes
    i18n.set_language("en")
    assert main(["--simulate-hp", "effects"]) == 0
    assert "Rainbow wave" in capsys.readouterr().out


def test_cli_errors_are_translated(capsys):
    from openalloy.presentation.cli.main import main
    with pytest.raises(SystemExit):
        main(["--lang", "pt_BR", "brightness", "1", "2"])
    assert "argumentos não reconhecidos" in capsys.readouterr().err


def test_language_command_saves_without_touching_the_keyboard(tmp_path, capsys):
    from openalloy.config.settings import Settings
    from openalloy.presentation.cli.main import main
    assert main(["language", "pt-BR"]) == 0
    assert Settings().get("language") == "pt_BR"
    assert "Idioma: Português (Brasil)" in capsys.readouterr().out
    i18n.set_language("en")
    assert main(["language"]) == 0                       # list shows the saved choice
    assert "* pt_BR" in capsys.readouterr().out


def test_service_switches_language_with_the_window(tmp_path):
    from openalloy.application.backend import make_handlers
    from openalloy.application.engine import Engine
    from openalloy.config.settings import PresetStore, Settings
    from openalloy.infra.drivers import open_driver
    from openalloy.infra.transport import DeviceNotFound

    def missing():
        raise DeviceNotFound(19, "x")
    e = Engine(missing, Settings(tmp_path / "c.json"), PresetStore(tmp_path / "p"),
               enable_input=False)
    e.start()
    try:
        deadline = time.time() + 3
        while not e.status()["error"] and time.time() < deadline:
            time.sleep(0.02)
        assert e.status()["error"].startswith("Keyboard not detected")
        make_handlers(e)["settings_update"](language="pt_BR")
        assert i18n.current() == "pt_BR"
        assert e.status()["error"].startswith("Teclado não detectado")   # same error, new language
    finally:
        e.stop()
    assert open_driver  # imported for parity with the other engine tests
