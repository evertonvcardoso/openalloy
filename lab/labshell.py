#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Interactive lab session for debugging: keeps one VM + emulator alive.

    python3 lab/labshell.py serve --zip openalloy-X.zip [--work DIR]   # boots, installs
    python3 lab/labshell.py "lab.oa('status')['out']"                   # evaluates in it
    python3 lab/labshell.py stop

Snippets run with ``lab`` (labkit.Lab), ``HP``, ``KINGSTON`` and ``run_lab`` in scope;
the value of the last expression is printed.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import socket
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
SOCK = Path("/tmp/labshell.sock")


def serve(zip_path: Path, work: Path) -> None:
    import run_lab
    from labkit import HP, KINGSTON, Lab
    lab = Lab(work, work / "results" / "shell", "shell-" + zip_path.stem.split("-")[-1])
    lab.boot()
    lab.wait(lambda: "seat0" in lab.run("loginctl list-sessions --no-legend")["out"], 90)
    run_lab.sc_install(lab, zip_path)
    scope = {"lab": lab, "HP": HP, "KINGSTON": KINGSTON, "run_lab": run_lab, "time": time,
             "json": json}
    if SOCK.exists():
        SOCK.unlink()
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(SOCK))
    srv.listen(1)
    print("labshell ready", flush=True)
    while True:
        conn, _a = srv.accept()
        code = b""
        while not code.endswith(b"\0"):
            part = conn.recv(65536)
            if not part:
                break
            code += part
        code_s = code.rstrip(b"\0").decode()
        if code_s == "__stop__":
            conn.sendall(b"stopping\0")
            conn.close()
            lab.close()
            return
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                tree = ast.parse(code_s)
                last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
                exec(compile(tree, "<labshell>", "exec"), scope)
                if last is not None:
                    val = eval(compile(ast.Expression(last.value), "<labshell>", "eval"), scope)
                    if val is not None:
                        print(val if isinstance(val, str) else json.dumps(val, indent=1, default=str))
        except Exception:
            buf.write(traceback.format_exc())
        conn.sendall(buf.getvalue().encode() + b"\0")
        conn.close()


def client(code: str) -> None:
    s = socket.socket(socket.AF_UNIX)
    s.connect(str(SOCK))
    s.sendall(code.encode() + b"\0")
    out = b""
    while not out.endswith(b"\0"):
        part = s.recv(65536)
        if not part:
            break
        out += part
    sys.stdout.write(out.rstrip(b"\0").decode())
    if not out.endswith(b"\n"):
        print()


if __name__ == "__main__":
    if sys.argv[1:2] == ["serve"]:
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("serve")
        ap.add_argument("--zip", required=True)
        ap.add_argument("--work", default=str(HERE.parent.parent / "lab-work"))
        a = ap.parse_args()
        serve(Path(a.zip).resolve(), Path(a.work))
    elif sys.argv[1:2] == ["stop"]:
        client("__stop__")
    else:
        client(sys.argv[1])
