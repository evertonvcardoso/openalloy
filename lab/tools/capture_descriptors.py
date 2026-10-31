#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Capture the real keyboard's USB/HID descriptors - READ ONLY.

Run on the machine with the keyboard plugged in (no sudo needed):

    python3 capture_descriptors.py > alloy-descriptors.json

Reads sysfs only (report_descriptor, uevent, idVendor, bcdDevice, bInterfaceNumber,
interface class/protocol, endpoint descriptors). It never opens /dev/hidraw* and never
sends anything to the keyboard. The output lets the lab replace its reconstructed
descriptors (lab/hidemu/hid.py) with the real ones.
"""

import glob
import json
import os
import sys

IDS = {("0951", "16e6"), ("03f0", "098f")}


def read(path, binary=False):
    try:
        with open(path, "rb" if binary else "r") as fh:
            data = fh.read()
        return data if binary else data.strip()
    except OSError:
        return None


def main():
    out = {"devices": {}}
    for dev in sorted(glob.glob("/sys/bus/usb/devices/*")):
        ids = (read(dev + "/idVendor"), read(dev + "/idProduct"))
        if ids not in IDS:
            continue
        info = {k: read(f"{dev}/{k}") for k in ("idVendor", "idProduct", "bcdDevice", "manufacturer",
                                                 "product", "speed", "bNumInterfaces", "bmAttributes",
                                                 "bMaxPower", "version")}
        desc = read(dev + "/descriptors", binary=True)
        info["raw_usb_descriptors"] = desc.hex() if desc else None
        info["interfaces"] = {}
        for itf in sorted(glob.glob(dev + "/" + os.path.basename(dev) + ":*")):
            n = read(itf + "/bInterfaceNumber")
            entry = {k: read(f"{itf}/{k}") for k in ("bInterfaceClass", "bInterfaceSubClass",
                                                      "bInterfaceProtocol", "bNumEndpoints")}
            entry["endpoints"] = []
            for ep in sorted(glob.glob(itf + "/ep_*")):
                entry["endpoints"].append({k: read(f"{ep}/{k}") for k in (
                    "bEndpointAddress", "bmAttributes", "wMaxPacketSize", "bInterval")})
            for hd in glob.glob(itf + "/*:*.*/report_descriptor"):
                rd = read(hd, binary=True)
                entry["report_descriptor"] = rd.hex() if rd else None
                hdir = os.path.dirname(hd)
                entry["hidraw"] = [os.path.basename(p) for p in glob.glob(hdir + "/hidraw/hidraw*")]
                entry["uevent"] = read(hdir + "/uevent")
            info["interfaces"][n] = entry
        out["devices"][os.path.basename(dev)] = info
    if not out["devices"]:
        print("no HyperX Alloy Origins Core (0951:16e6 or 03f0:098f) found", file=sys.stderr)
        return 1
    json.dump(out, sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
