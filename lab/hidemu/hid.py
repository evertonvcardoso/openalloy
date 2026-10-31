# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""USB and HID descriptors of the emulated keyboards.

Fidelity (stated, not assumed):
  * USB ids, interface numbers, bcdDevice (firmware), product strings: from the
    project (protocol.py, udev rules) and the journal of the real HP keyboard
    ("HP, Inc HyperX Alloy Origins Core", firmware 21.16, hidraw on if2 and if3).
  * Interface 2 report descriptor: vendor page 0xFF13, 64-byte input/output reports
    without report ids - as documented in protocol.py. Not captured from hardware.
  * Interface 3 report descriptor: built from the HID Usage Tables LampArray page
    (0x59) with report ids 1-6 and the field layout the driver documents. Not a
    byte-for-byte capture of the HP keyboard: run lab/tools/capture_descriptors.py on
    the real machine to replace it with the real one.
  * Interfaces 0/1: a standard boot keyboard and a consumer-control interface.
"""

from __future__ import annotations

import struct
from typing import List, Tuple

# --- HID report descriptors -----------------------------------------------------------

BOOT_KEYBOARD = bytes.fromhex(
    "05010906a101050719e029e71500250175019508810295017508810195057501"
    "05081901290591029501750391019506750815002cff05071900 29ff8100c0".replace(" ", "")
    .replace("2cff", "25ff"))

CONSUMER = bytes.fromhex("050c0901a101150026ff031900 2aff03751095018100c0".replace(" ", ""))

VENDOR_FF13 = bytes.fromhex(
    "0613ff0901a1011500 26ff0075089540 0902 8102 0903 9102 c0".replace(" ", ""))


def _u16max() -> bytes:
    return bytes.fromhex("27ffff0000")


def _i32max() -> bytes:
    return bytes.fromhex("27ffffff7f")


def lamparray() -> bytes:
    d = bytearray()
    d += bytes.fromhex("0559 0901 a101".replace(" ", ""))
    # 1 LampArrayAttributesReport (feature, read-only)
    d += bytes.fromhex("8501 0902 a102".replace(" ", ""))
    d += bytes.fromhex("0903 1500".replace(" ", "")) + _u16max() + bytes.fromhex("75109501b103")
    d += bytes.fromhex("0904 0905 0906 0907 0908 1500".replace(" ", "")) + _i32max() \
        + bytes.fromhex("75209505b103")
    d += b"\xc0"
    # 2 LampAttributesRequestReport
    d += bytes.fromhex("8502 0920 a102 0921 1500".replace(" ", "")) + _u16max() \
        + bytes.fromhex("75109501b102c0")
    # 3 LampAttributesResponseReport
    d += bytes.fromhex("8503 0922 a102 0921 1500".replace(" ", "")) + _u16max() \
        + bytes.fromhex("75109501b102")
    d += bytes.fromhex("0923 0924 0925 0927 0926 1500".replace(" ", "")) + _i32max() \
        + bytes.fromhex("75209505b102")
    d += bytes.fromhex("0928 0929 092a 092b 092c 092d 1500 26ff00 75089506b102 c0".replace(" ", ""))
    # 4 LampMultiUpdateReport: count, flags, 8 ids, 8 x RGBI
    d += bytes.fromhex("8504 0950 a102 0903 0955 1500 2508 75089502b102".replace(" ", ""))
    d += bytes.fromhex("0921 1500".replace(" ", "")) + _u16max() + bytes.fromhex("75109508b102")
    d += bytes.fromhex("09510952 09530954".replace(" ", "")) * 8
    d += bytes.fromhex("1500 26ff00 75089520b102 c0".replace(" ", ""))
    # 5 LampRangeUpdateReport: flags, first, last, R, G, B, I
    d += bytes.fromhex("8505 0960 a102 0955 1500 2508 75089501b102".replace(" ", ""))
    d += bytes.fromhex("0961 0962 1500".replace(" ", "")) + _u16max() + bytes.fromhex("75109502b102")
    d += bytes.fromhex("0951 0952 0953 0954 1500 26ff00 75089504b102 c0".replace(" ", ""))
    # 6 LampArrayControlReport: AutonomousMode
    d += bytes.fromhex("8506 0970 a102 0971 1500 2501 75089501b102 c0".replace(" ", ""))
    d += b"\xc0"
    return bytes(d)


LAMPARRAY = lamparray()

# Feature report sizes (report id included), as declared above.
LAMP_REPORT_SIZES = {1: 23, 2: 3, 3: 29, 4: 51, 5: 10, 6: 2}

# --- USB descriptors ------------------------------------------------------------------

USB_DT_DEVICE, USB_DT_CONFIG, USB_DT_STRING, USB_DT_INTERFACE, USB_DT_ENDPOINT = 1, 2, 3, 4, 5
HID_DT_HID, HID_DT_REPORT = 0x21, 0x22


class Endpoint:
    def __init__(self, address: int, max_packet: int, interval: int = 1):
        self.address = address
        self.max_packet = max_packet
        self.interval = interval

    @property
    def is_in(self) -> bool:
        return bool(self.address & 0x80)

    def descriptor(self) -> bytes:
        return struct.pack("<BBBBHB", 7, USB_DT_ENDPOINT, self.address, 0x03,
                           self.max_packet, self.interval)


class Interface:
    def __init__(self, number: int, report_descriptor: bytes, endpoints: List[Endpoint],
                 subclass: int = 0, protocol: int = 0):
        self.number = number
        self.report_descriptor = report_descriptor
        self.endpoints = endpoints
        self.subclass = subclass
        self.protocol = protocol

    def hid_descriptor(self) -> bytes:
        return struct.pack("<BBHBBBH", 9, HID_DT_HID, 0x0111, 0, 1, HID_DT_REPORT,
                           len(self.report_descriptor))

    def descriptor(self) -> bytes:
        head = struct.pack("<BBBBBBBBB", 9, USB_DT_INTERFACE, self.number, 0,
                           len(self.endpoints), 0x03, self.subclass, self.protocol, 0)
        return head + self.hid_descriptor() + b"".join(e.descriptor() for e in self.endpoints)


def device_descriptor(vid: int, pid: int, bcd: int) -> bytes:
    return struct.pack("<BBHBBBBHHHBBBB", 18, USB_DT_DEVICE, 0x0200, 0, 0, 0, 64,
                       vid, pid, bcd, 1, 2, 3, 1)


def config_descriptor(interfaces: List[Interface]) -> bytes:
    body = b"".join(i.descriptor() for i in interfaces)
    # bus powered + remote wake-up (a keyboard can wake the PC), 500 mA
    head = struct.pack("<BBHBBBBB", 9, USB_DT_CONFIG, 9 + len(body), len(interfaces), 1, 0,
                       0xA0, 250)
    return head + body


def string_descriptor(text: str) -> bytes:
    data = text.encode("utf-16-le")
    return bytes([2 + len(data), USB_DT_STRING]) + data


LANGIDS = bytes([4, USB_DT_STRING, 0x09, 0x04])


# Interface 3 of the Kingston model: what it really carries is not documented in the
# project; a plain vendor collection (page 0xFF00) stands in for it.
VENDOR_FF00 = bytes.fromhex("0600ff0901a101150026ff0075089540090281 02c0".replace(" ", ""))


def standard_interfaces(lamparray: bool = True) -> List[Interface]:
    return [
        Interface(0, BOOT_KEYBOARD, [Endpoint(0x81, 8, 1)], subclass=1, protocol=1),
        Interface(1, CONSUMER, [Endpoint(0x82, 8, 1)]),
        Interface(2, VENDOR_FF13, [Endpoint(0x83, 64, 1), Endpoint(0x04, 64, 1)]),
        Interface(3, LAMPARRAY if lamparray else VENDOR_FF00, [Endpoint(0x85, 64, 4)]),
    ]


def parse_items(desc: bytes) -> List[Tuple[int, int, int]]:
    """(tag/type byte without size, size, value) - a small sanity parser for tests."""
    out = []
    i = 0
    while i < len(desc):
        b = desc[i]
        size = (0, 1, 2, 4)[b & 3]
        val = int.from_bytes(desc[i + 1:i + 1 + size], "little") if size else 0
        out.append((b & 0xFC, size, val))
        i += 1 + size
    if i != len(desc):
        raise ValueError("truncated descriptor")
    return out
