# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""GUI: results from worker threads must not starve behind the preview animation.

The keyboard preview redraws on the frame clock; on a slow or busy machine the main
loop is never idle, so callbacks queued at idle priority never ran (lab, 1.5.3: a
language switch from the window was saved by the service but never applied)."""
import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "openalloy" / "presentation" / "gui" / "app.py"


def test_every_idle_add_in_the_window_uses_default_priority():
    calls = [n for n in ast.walk(ast.parse(APP.read_text()))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "idle_add"]
    assert calls
    for c in calls:
        kw = {k.arg: ast.unparse(k.value) for k in c.keywords}
        assert kw.get("priority") == "GLib.PRIORITY_DEFAULT", f"line {c.lineno}"
