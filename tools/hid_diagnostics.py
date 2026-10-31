#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""OpenAlloy - READ-ONLY diagnostics for the HyperX Alloy Origins Core.

Does not open the keyboard and sends no command. Reads sysfs files only:
  - the HID report descriptor of each interface (report_descriptor)
  - the USB endpoints of each interface
and shows, per interface: usage pages, report ids and INPUT / OUTPUT / FEATURE
sizes. That tells on which interface, and in which format, the keyboard accepts
lighting commands.

Usage:  python3 tools/hid_diagnostics.py            (no sudo needed)
        python3 tools/hid_diagnostics.py > diag.txt  (to send the result)

Runs without installing OpenAlloy; when the package sits next to it, messages
follow the OpenAlloy language (--lang, $OPENALLOY_LANG or the saved preference).
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from openalloy import i18n as _i18n
    _i18n.set_language(_i18n.resolve(sys.argv[2] if sys.argv[1:2] == ["--lang"] else None))
    _ = _i18n._
except Exception:            # standalone copy: English
    def _(message):
        return message

IDS = {("03f0", "098f"), ("0951", "16e6")}


def read(path, binary=False):
    try:
        with open(path, "rb" if binary else "r") as fh:
            return fh.read() if binary else fh.read().strip()
    except OSError:
        return b"" if binary else ""


def parse_descriptor(d):
    """Minimal HID report-descriptor parser: collections, usage pages, report IDs and
    the size in bytes of every INPUT/OUTPUT/FEATURE report."""
    report_size = report_count = 0
    report_id = 0
    pages = []
    reports = {}          # (kind, report_id) -> bits
    i = 0
    while i < len(d):
        b = d[i]
        if b == 0xFE:                       # long item
            size = d[i + 1] if i + 1 < len(d) else 0
            i += 3 + size
            continue
        size = (0, 1, 2, 4)[b & 3]
        val = int.from_bytes(d[i + 1:i + 1 + size], "little")
        tag, typ = b & 0xFC, (b >> 2) & 3
        i += 1 + size
        if typ == 1:                         # global
            if tag == 0x04:
                if val not in pages:
                    pages.append(val)
            elif tag == 0x74:
                report_size = val
            elif tag == 0x94:
                report_count = val
            elif tag == 0x84:
                report_id = val
        elif typ == 0 and tag in (0x80, 0x90, 0xB0):   # main: input/output/feature
            kind = {0x80: "INPUT", 0x90: "OUTPUT", 0xB0: "FEATURE"}[tag]
            key = (kind, report_id)
            reports[key] = reports.get(key, 0) + report_size * report_count
    return pages, reports


def endpoints(intf_dir):
    out = []
    for ep in sorted(glob.glob(os.path.join(intf_dir, "ep_*"))):
        addr = read(os.path.join(ep, "bEndpointAddress"))
        etype = read(os.path.join(ep, "type"))
        direc = read(os.path.join(ep, "direction"))
        size = read(os.path.join(ep, "wMaxPacketSize"))
        out.append(f"{addr} {etype} {direc} max={size}")
    return out


def main():
    found = False
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        uevent = read(os.path.join(node, "device", "uevent"))
        fields = dict(ln.split("=", 1) for ln in uevent.splitlines() if "=" in ln)
        hid_id = fields.get("HID_ID", "")
        try:
            _bus, vid, pid = hid_id.split(":")
        except ValueError:
            continue
        vid, pid = vid[-4:].lower(), pid[-4:].lower()
        if (vid, pid) not in IDS:
            continue
        found = True
        dev = os.path.realpath(os.path.join(node, "device"))
        intf = os.path.dirname(dev)
        iface = read(os.path.join(intf, "bInterfaceNumber"))
        desc = read(os.path.join(dev, "report_descriptor"), binary=True)
        pages, reports = parse_descriptor(desc)
        print("=" * 72)
        print(f"/dev/{os.path.basename(node)}  {vid}:{pid}  interface {iface}  "
              f"({fields.get('HID_NAME', '')})")
        print(f"  classe/sub/proto : {read(os.path.join(intf, 'bInterfaceClass'))}/"
              f"{read(os.path.join(intf, 'bInterfaceSubClass'))}/"
              f"{read(os.path.join(intf, 'bInterfaceProtocol'))}")
        print("  " + _("usage pages      : ") + ", ".join(f"0x{p:04X}" + (" " + _("(vendor)") if p >= 0xFF00 else "")
                                              for p in pages))
        for (kind, rid), bits in sorted(reports.items()):
            print(f"  {kind:<7} id={rid:<3} {bits // 8:>4} bytes")
        for ep in endpoints(intf):
            print(f"  endpoint         : {ep}")
        print("  " + _("descriptor ({size} bytes): {data}").format(size=len(desc), data=desc.hex(" ")))
    usb = [d for d in glob.glob("/sys/bus/usb/devices/*")
           if (read(d + "/idVendor"), read(d + "/idProduct")) in IDS]
    for d in usb:
        print("=" * 72)
        print(f"USB {read(d + '/idVendor')}:{read(d + '/idProduct')}  "
              f"firmware(bcdDevice)={read(d + '/bcdDevice')}  "
              + _("product='{product}'  interfaces={count}").format(
                  product=read(d + "/product"), count=read(d + "/bNumInterfaces")))
        for intf in sorted(glob.glob(d + "/*:*")):
            drv = os.path.basename(os.path.realpath(intf + "/driver")) if os.path.exists(intf + "/driver") else _("(no driver)")
            print("  " + _("interface {number}: class {cls} driver={driver}  endpoints: {endpoints}")
                  .format(number=read(intf + "/bInterfaceNumber"), cls=read(intf + "/bInterfaceClass"),
                          driver=drv, endpoints="; ".join(endpoints(intf))))
    if not found:
        print(_("No HyperX Alloy Origins Core found."))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
