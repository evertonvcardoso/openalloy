#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Compare two lab runs: python3 lab/compare.py results/OLD results/NEW > comparison.md"""

import json
import sys
from pathlib import Path


def load(d):
    return json.loads((Path(d) / "results.json").read_text())


def mark(ok):
    return {True: "PASS", False: "FAIL", None: "-"}[ok]


def main(a, b):
    A, B = load(a), load(b)
    ra = {(r["scenario"], r["check"]): r for r in A["results"] if r["ok"] is not None}
    rb = {(r["scenario"], r["check"]): r for r in B["results"] if r["ok"] is not None}
    keys = list(dict.fromkeys(list(ra) + list(rb)))
    print(f"# Lab comparison: {A['label']} vs {B['label']}\n")
    for lab in (A, B):
        res = [r for r in lab["results"] if r["ok"] is not None]
        print(f"- **{lab['label']}**: {sum(r['ok'] for r in res)} passed, "
              f"{sum(not r['ok'] for r in res)} failed, {lab['seconds']} s")
    print("\n## Differences\n")
    diff = [k for k in keys if (ra.get(k) or {}).get("ok") != (rb.get(k) or {}).get("ok")]
    if not diff:
        print("None: every check has the same result.\n")
    else:
        print(f"| Scenario | Check | {A['label']} | {B['label']} | Detail ({B['label']}) |")
        print("|---|---|---|---|---|")
        for k in diff:
            x, y = ra.get(k), rb.get(k)
            print(f"| {k[0]} | {k[1]} | {mark(x['ok']) if x else 'n/a'} | {mark(y['ok']) if y else 'n/a'} "
                  f"| {((y or x)['detail'] or '')[:160].replace('|', '/')} |")
    print("\n## Metrics\n")
    print(f"| Metric | {A['label']} | {B['label']} |\n|---|---|---|")
    for m in sorted(set(A["metrics"]) | set(B["metrics"])):
        if m in ("guest", "screenshots", "suspend_lines"):
            continue
        print(f"| {m} | {json.dumps(A['metrics'].get(m))[:120]} | {json.dumps(B['metrics'].get(m))[:120]} |")
    print("\n## Suspend journal lines\n")
    for lab in (A, B):
        print(f"**{lab['label']}**\n```")
        for ln in lab["metrics"].get("suspend_lines", []):
            print(ln)
        print("```")
    print(f"\n## All checks\n\n| Scenario | Check | {A['label']} | {B['label']} |\n|---|---|---|---|")
    for k in keys:
        x, y = ra.get(k), rb.get(k)
        print(f"| {k[0]} | {k[1]} | {mark(x['ok']) if x else 'n/a'} | {mark(y['ok']) if y else 'n/a'} |")


if __name__ == "__main__":
    main(*sys.argv[1:3])
