# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""python3 -m hidemu serve --kingston 1-1 --hp 1-2 [--variant abnt2] [--control PATH]

Runs the USB/IP server (port 3240) with the requested emulated keyboards and the
control socket. Other subcommands talk to a running emulator:
  state BUSID | log BUSID [--since N] | fault BUSID NAME [VALUE] | plug/unplug BUSID |
  press BUSID USAGE | render BUSID > out.svg
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path

from . import devices
from .control import ControlClient, ControlServer
from .usbip import UsbipServer

DEFAULT_CONTROL = "/tmp/hidemu.sock"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hidemu")
    ap.add_argument("--control", default=DEFAULT_CONTROL)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--kingston", metavar="BUSID")
    s.add_argument("--hp", metavar="BUSID")
    s.add_argument("--variant", default="abnt2", choices=["abnt2", "ansi", "iso"])
    s.add_argument("--host", default="127.0.0.1",
                   help="address to listen on (default: loopback only, enough for a QEMU "
                        "guest with user networking; 0.0.0.0 exposes the emulated keyboards "
                        "to the whole network)")
    s.add_argument("--port", type=int, default=3240)
    s.add_argument("--min-update-us", type=int, default=30000)
    for name in ("state", "plug", "unplug", "render"):
        p = sub.add_parser(name)
        p.add_argument("busid")
    p = sub.add_parser("log")
    p.add_argument("busid")
    p.add_argument("--since", type=int, default=0)
    p = sub.add_parser("fault")
    p.add_argument("busid")
    p.add_argument("name")
    p.add_argument("value", nargs="?")
    p = sub.add_parser("press")
    p.add_argument("busid")
    p.add_argument("usage", type=lambda x: int(x, 0))
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
        devs = []
        if a.kingston:
            devs.append(devices.VendorKeyboard(busid=a.kingston, variant=a.variant))
        if a.hp:
            devs.append(devices.HpKeyboard(busid=a.hp, variant=a.variant,
                                           min_update_us=a.min_update_us))
        if not devs:
            ap.error("give --kingston and/or --hp")
        srv = UsbipServer(devs, host=a.host, port=a.port)
        srv.start()
        ctl = ControlServer(srv, Path(a.control))
        ctl.start()
        print(json.dumps({"ready": True, "devices": [d.busid for d in devs],
                          "control": a.control}), flush=True)
        stop = []
        signal.signal(signal.SIGTERM, lambda *_x: stop.append(1))
        signal.signal(signal.SIGINT, lambda *_x: stop.append(1))
        while not stop:
            time.sleep(0.2)
        srv.stop()
        return 0

    c = ControlClient(Path(a.control))
    if a.cmd == "render":
        sys.stdout.write(c("render", busid=a.busid))
        return 0
    if a.cmd == "log":
        for e in c("log", busid=a.busid, since=a.since):
            print(f"{e['seq']:>7} {e['t']:>10.4f} {e['kind']:<12} if{e['if']} "
                  f"{e['status']:>4} {e['note']:<40} {e['hex']}")
        return 0
    if a.cmd == "fault":
        value = None if a.value is None else json.loads(a.value)
        print(json.dumps(c("fault", busid=a.busid, name=a.name, value=value)))
        return 0
    if a.cmd == "press":
        c("press_key", busid=a.busid, usage=a.usage)
        return 0
    print(json.dumps(c(a.cmd, busid=a.busid), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
