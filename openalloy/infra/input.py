# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Key-press source for reactive effects, using raw evdev (stdlib only).

Only the Alloy Origins Core's own event nodes are opened (matched by USB id
through sysfs) and they are never grabbed, so typing is unaffected. Reading
requires either membership of the ``input`` group or the optional udev rule
installed by ``install.sh --reactive``. Without access the effect still
works from the firmware's 0x73 key reports when the keyboard sends them.
"""

from __future__ import annotations

import logging
import os
import select
import struct
import threading
import time
from typing import Callable, Dict, List, Set

from openalloy.i18n import _, ngettext

log = logging.getLogger(__name__)

EV_KEY = 0x01
_EVENT = struct.Struct("llHHi")   # struct input_event on 64-bit Linux
SYSFS_INPUT = "/sys/class/input"


def _read(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def find_event_nodes(ids) -> List[str]:
    wanted = {(f"{v:04x}", f"{p:04x}") for v, p in ids}
    out = []
    try:
        entries = sorted(os.listdir(SYSFS_INPUT))
    except OSError:
        return out
    for e in entries:
        if not e.startswith("event"):
            continue
        base = os.path.join(SYSFS_INPUT, e, "device", "id")
        if (_read(os.path.join(base, "vendor")), _read(os.path.join(base, "product"))) in wanted:
            out.append("/dev/input/" + e)
    return out


class EvdevKeySource:
    def __init__(self, ids, on_press: Callable[[int], None]):
        self._ids = ids
        self._on_press = on_press
        self._stop = threading.Event()
        self._thread = None
        self.status = _("stopped")
        self.denied: Set[str] = set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            if not self._stop.is_set():
                return
            self._thread.join(timeout=1.0)   # a stop is in progress; let it finish
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="openalloy-evdev", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _open_all(self) -> Dict[int, str]:
        fds: Dict[int, str] = {}
        self.denied.clear()
        for node in find_event_nodes(self._ids):
            try:
                fds[os.open(node, os.O_RDONLY | os.O_NONBLOCK)] = node
            except PermissionError:
                self.denied.add(node)
            except OSError:
                pass
        if fds:
            self.status = ngettext("active ({n} device)", "active ({n} devices)",
                                   len(fds)).format(n=len(fds))
        elif self.denied:
            self.status = _("no permission on {nodes}").format(nodes=", ".join(sorted(self.denied)))
        else:
            self.status = _("keyboard not found")
        return fds

    def _run(self) -> None:
        fds: Dict[int, str] = {}
        last_scan = 0.0
        try:
            while not self._stop.is_set():
                if not fds and time.monotonic() - last_scan > 3.0:
                    fds = self._open_all()
                    last_scan = time.monotonic()
                if not fds:
                    self._stop.wait(0.5)
                    continue
                ready, _w, _x = select.select(list(fds), [], [], 0.25)
                for fd in ready:
                    try:
                        data = os.read(fd, _EVENT.size * 64)
                    except OSError:
                        os.close(fd)
                        fds.pop(fd, None)
                        continue
                    for off in range(0, len(data) - _EVENT.size + 1, _EVENT.size):
                        _s, _us, etype, code, value = _EVENT.unpack_from(data, off)
                        if etype == EV_KEY and value == 1:
                            try:
                                self._on_press(code)
                            except Exception:
                                log.exception("key handler failed")
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self.status = _("stopped")
