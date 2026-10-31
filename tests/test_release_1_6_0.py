# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""1.6.0: GNU project files, authorship, manual pages and English documentation."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

import openalloy

ROOT = Path(__file__).resolve().parent.parent
AUTHOR = "Everton Vilhena Cardoso"
GNU_FILES = ("README.md", "INSTALL", "NEWS", "ChangeLog", "AUTHORS", "THANKS", "COPYING")
MAN_PAGES = ("openalloy.1", "openalloyd.1")


def _pyproject() -> str:
    return (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_gnu_project_files_exist_and_old_ones_are_gone():
    for name in GNU_FILES:
        assert (ROOT / name).is_file(), name
    for old in ("LICENSE", "NOTICE", "CHANGELOG.md", "PRIMEIROS_PASSOS_SPARKYLINUX.md"):
        assert not (ROOT / old).exists(), old
    assert sorted(p.name for p in (ROOT / "docs").glob("*.md")) == \
        ["DEVELOPMENT.md", "PROTOCOL.md", "TESTING.md"]


def test_copying_is_the_gnu_gpl_version_2():
    text = (ROOT / "COPYING").read_text(encoding="utf-8")
    assert text.lstrip().startswith("GNU GENERAL PUBLIC LICENSE")
    assert "Version 2, June 1991" in text
    assert "END OF TERMS AND CONDITIONS" in text
    assert 'license = "GPL-2.0-or-later"' in _pyproject()


def test_one_version_everywhere():
    v = openalloy.__version__
    assert re.search(rf'^version = "{re.escape(v)}"$', _pyproject(), re.M)
    for page in MAN_PAGES:
        th = next(line for line in (ROOT / "man" / page).read_text().splitlines()
                  if line.startswith(".TH"))
        assert f'"OpenAlloy {v}"' in th, page
    assert f"release {v} " in (ROOT / "NEWS").read_text(encoding="utf-8").split("\n* ", 2)[1]
    assert f"Release {v}" in (ROOT / "ChangeLog").read_text(encoding="utf-8").split("\n\n")[1]


def test_authorship():
    py = _pyproject()
    assert f'authors = [{{ name = "{AUTHOR}" }}]' in py
    assert f'maintainers = [{{ name = "{AUTHOR}" }}]' in py
    assert "contributors" not in py
    assert AUTHOR in (ROOT / "AUTHORS").read_text(encoding="utf-8")
    assert f"Copyright (C) 2026 {AUTHOR}" in (ROOT / "THANKS").read_text(encoding="utf-8")
    assert f"Copyright © 2026 {AUTHOR}" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_mit_notice_kept_for_reused_code():
    thanks = (ROOT / "THANKS").read_text(encoding="utf-8")
    for project in ("Linuity", "PlasmaNGenuity", "hyperx-studio", "OpenRGB", "hxoctkl"):
        assert project in thanks
    assert "The above copyright notice and this permission notice shall be included" in thanks


def _sources():
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or ".pytest_cache" in p.parts:
            continue
        if p.suffix in (".py", ".sh", ".rules", ".service") or p.name in ("openalloy-sleep",
                                                                           "zz-lab-usbip"):
            yield p


def test_every_source_file_has_a_license_header():
    missing = [str(p.relative_to(ROOT)) for p in _sources()
               if "SPDX-License-Identifier: GPL-2.0-or-later" not in "".join(
                   p.read_text(encoding="utf-8").splitlines(True)[:4])
               or f"Copyright (C) 2026 {AUTHOR}" not in p.read_text(encoding="utf-8")]
    assert missing == []


def test_install_and_uninstall_handle_the_manual_pages():
    inst = (ROOT / "install.sh").read_text(encoding="utf-8")
    unin = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
    assert 'MAN_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/man/man1"' in inst
    assert '"$SRC/man/openalloy.1" "$SRC/man/openalloyd.1"' in inst
    for page in MAN_PAGES:
        assert f"/man/man1/{page}" in unin
    service = (ROOT / "openalloy" / "resources" / "openalloy.service").read_text()
    assert "Documentation=man:openalloyd(1) man:openalloy(1)" in service


def test_manifest_ships_the_new_files():
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    for name in GNU_FILES:
        assert name in manifest
    assert "recursive-include man *.1" in manifest
    assert 'license-files = ["COPYING", "THANKS"]' in _pyproject()


@pytest.mark.skipif(not shutil.which("groff"), reason="groff not installed")
@pytest.mark.parametrize("page", MAN_PAGES)
def test_manual_pages_render_without_warnings(page):
    r = subprocess.run(["groff", "-man", "-Tutf8", "-ww", "-z", str(ROOT / "man" / page)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stderr == ""


def test_manual_page_lists_every_command():
    from openalloy.presentation.cli.main import build_parser
    sub = next(a for a in build_parser()._actions if a.dest == "cmd")
    page = (ROOT / "man" / "openalloy.1").read_text(encoding="utf-8").replace("\\-", "-")
    missing = [c for c in sub.choices
               if not re.search(rf"^\.(?:B|BR|BI) {re.escape(c)}(?:\s|$)", page, re.M)]
    assert len(sub.choices) >= 18
    assert missing == []


def test_documentation_is_english():
    accented = re.compile("[ãõçáéíóúâêôà]")
    docs = [ROOT / "README.md", ROOT / "INSTALL", *(ROOT / "docs").glob("*.md"),
            *(ROOT / "man").glob("*.1")]
    found = {p.name: accented.findall(p.read_text(encoding="utf-8")) for p in docs}
    assert {k: v for k, v in found.items() if v} == {}


@pytest.mark.parametrize("bad", ["abc", "nan", "inf", "1e999", "", True])
def test_effect_number_parameters_reject_non_numbers_with_our_message(bad):
    """Lab finding: 'apply wave -p speed=abc' showed Python's untranslated
    "could not convert string to float" and 'nan' crashed with another raw message."""
    from openalloy.application.effects.library import REGISTRY
    from openalloy.i18n import Message
    spec = next(p for p in REGISTRY["wave"].params if p.name == "speed")
    with pytest.raises(ValueError) as err:
        spec._coerce(bad)
    assert isinstance(err.value.args[0], Message)
    assert str(err.value) == "speed: a number is expected"
