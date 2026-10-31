# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Control API of the emulator (JSON lines over a Unix socket) and its client.

Requests: {"op": ..., "busid": ..., ...}
  state, log (since, limit), fault (name, value | null to clear), clear_faults,
  plug, unplug, attached, press_key (usage), profile_key (profile), events, render.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path
from typing import Any

from . import render
from .usbip import UsbipServer


class ControlServer:
    def __init__(self, usbip: UsbipServer, path: Path):
        self.usbip = usbip
        self.path = Path(path)
        self.sock: socket.socket = socket.socket(socket.AF_UNIX)

    def start(self) -> None:
        if self.path.exists():
            self.path.unlink()
        self.sock.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self.sock.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _a = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        f = conn.makefile("rwb", buffering=0)
        for line in f:
            try:
                req = json.loads(line)
                res = {"ok": True, "result": self.handle(req)}
            except Exception as exc:              # reported to the caller
                res = {"ok": False, "error": repr(exc)}
            f.write((json.dumps(res) + "\n").encode())
        conn.close()

    def handle(self, req: dict) -> Any:
        op = req["op"]
        if op == "events":
            return self.usbip.events
        if op == "devices":
            return list(self.usbip.devices)
        dev = self.usbip.devices[req["busid"]]
        if op == "state":
            s = dev.state()
            s["attached"] = self.usbip.attached(dev.busid)
            return s
        if op == "log":
            return dev.log_since(int(req.get("since", 0)), int(req.get("limit", 5000)))
        if op == "fault":
            if req.get("value") is None:
                dev.faults.pop(req["name"], None)
            else:
                dev.faults[req["name"]] = req["value"]
            return dict(dev.faults)
        if op == "clear_faults":
            dev.faults.clear()
            return {}
        if op == "unplug":
            self.usbip.unplug(dev.busid)
            return True
        if op == "plug":
            self.usbip.plug(dev.busid)
            return True
        if op == "attached":
            return self.usbip.attached(dev.busid)
        if op == "press_key":
            dev.press_key(int(req["usage"]))
            return True
        if op == "profile_key":
            dev.profile_key(int(req["profile"]))
            return True
        if op == "seed_store":
            dev.seed_store(int(req["profile"]), bytes.fromhex(req["hex"]))
            return True
        if op == "render":
            return render.svg(dev, req.get("title", ""))
        raise ValueError(f"unknown op {op!r}")


class ControlClient:
    def __init__(self, path: Path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(str(path))
        self.f = self.sock.makefile("rwb", buffering=0)
        self.lock = threading.Lock()

    def __call__(self, op: str, **kw) -> Any:
        kw["op"] = op
        with self.lock:
            self.f.write((json.dumps(kw) + "\n").encode())
            res = json.loads(self.f.readline())
        if not res["ok"]:
            raise RuntimeError(res["error"])
        return res["result"]

    def close(self) -> None:
        self.sock.close()
