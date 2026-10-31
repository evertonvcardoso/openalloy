# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""OpenAlloy test lab - USB/HID emulation of the supported keyboards.

Nothing here is imported by the ``openalloy`` package; it is not part of the wheel.

Layers (see lab/README.md):
  * ``keytable``  - an independent physical key table (rows, sizes, HID usages)
  * ``hid``       - USB and HID descriptors
  * ``devices``   - behaviour of the two keyboards (vendor channel, LampArray),
                    with an observable state, a hex log and injectable faults
  * ``usbip``     - a USB/IP server: the devices appear as real USB devices to a
                    Linux kernel (vhci-hcd), with usbhid, hidraw, evdev and udev
  * ``control``   - JSON control API used by the test orchestrator
  * ``render``    - SVG picture of what the emulated keyboard shows
"""

__all__ = ["keytable", "hid", "devices", "usbip", "control", "render"]
