# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""1.6.2: the CI result must not depend on the runner's shellcheck version."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_ci_pins_shellcheck():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert re.search(r'"shellcheck-py==\d+\.\d+\.\d+\.\d+"', ci)
    assert "apt-get install -y -qq shellcheck" not in ci


def test_installer_has_no_and_or_chains():
    """SC2015: 'A && B || C' runs C when B fails; older shellcheck versions flag it."""
    for script in ("install.sh", "uninstall.sh"):
        for no, line in enumerate((ROOT / script).read_text().splitlines(), 1):
            code = line.split(" #", 1)[0]
            assert not re.search(r"&&[^|]*\|\|", code), f"{script}:{no}: {line.strip()}"
