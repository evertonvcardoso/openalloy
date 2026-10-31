# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""1.6.1: repository files (CI, security policy, issue forms), project URLs, and the lab
emulator listening on the loopback only."""

import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "https://github.com/evertonvcardoso/openalloy"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_project_urls_point_to_the_repository():
    py = _read("pyproject.toml")
    for key, url in (("Homepage", REPO), ("Source", REPO), ("Issues", f"{REPO}/issues"),
                     ("Releases", f"{REPO}/releases"), ("Changelog", f"{REPO}/blob/main/NEWS")):
        assert f'{key} = "{url}"' in py


def test_security_policy_reports_privately():
    text = _read("SECURITY.md")
    assert f"{REPO}/security/advisories/new" in text
    assert "do not open a public issue" in text
    assert "SECURITY.md" in _read("MANIFEST.in")
    assert "SECURITY.md" in _read("README.md")


def test_issue_forms_ask_for_diagnostics_and_route_security_reports():
    bug = _read(".github/ISSUE_TEMPLATE/bug_report.yml")
    for needle in ("openalloy --version", "openalloy doctor", "journalctl --user -u openalloy"):
        assert needle in bug
    config = _read(".github/ISSUE_TEMPLATE/config.yml")
    assert "blank_issues_enabled: false" in config
    assert f"{REPO}/security/advisories/new" in config


def test_ci_runs_every_supported_python_with_read_only_permissions():
    ci = _read(".github/workflows/ci.yml")
    assert re.search(r"^permissions:\n  contents: read$", ci, re.M)
    assert 'python: ["3.10", "3.11", "3.12", "3.13", "3.14"]' in ci
    assert 'requires-python = ">=3.10"' in _read("pyproject.toml")
    for step in ("python -m pytest", "lab/tests", "ruff check", "tools/i18n.py check",
                 "shellcheck", "groff -man"):
        assert step in ci
    assert "persist-credentials: false" in ci
    assert "secrets." not in ci


def test_readme_badges_and_download_link():
    readme = _read("README.md")
    assert f"[![Tests]({REPO}/actions/workflows/ci.yml/badge.svg)]" in readme
    assert f"({REPO}/releases/latest)" in readme
    assert "openalloy-1.6.0.zip" not in readme          # no stale version in the instructions


def test_lab_emulator_listens_on_the_loopback_by_default():
    sys.path.insert(0, str(ROOT / "lab"))
    try:
        from hidemu.__main__ import main
        from hidemu.usbip import UsbipServer
    finally:
        sys.path.remove(str(ROOT / "lab"))
    assert inspect.signature(UsbipServer).parameters["host"].default == "127.0.0.1"
    src = inspect.getsource(main)
    assert '"--host", default="127.0.0.1"' in src
    assert "host=a.host" in src
