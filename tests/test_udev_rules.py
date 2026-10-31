# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""udev rules: they must be able to match at all, and open only the lighting interfaces.

udev evaluates every ATTRS{}/KERNELS/SUBSYSTEMS/DRIVERS key of one rule against the
SAME parent device. The USB ids are on the USB device and bInterfaceNumber on the USB
interface, so a rule mixing them can never match - which is what OpenAlloy <= 1.5.3
shipped (found by the lab: no uaccess tag, "permission denied" on a clean system).
The real-udev check is lab/run_lab.py (scenario "permissions").
"""
import re
from pathlib import Path

RES = Path(__file__).resolve().parent.parent / "openalloy" / "resources"
PARENT_KEYS = re.compile(r'\b(ATTRS|KERNELS|SUBSYSTEMS|DRIVERS)\{?([^}=!]*)\}?\s*[=!]=')
DEVICE_ATTRS = {"idVendor", "idProduct", "bcdDevice", "manufacturer", "product", "serial"}
INTERFACE_ATTRS = {"bInterfaceNumber", "bInterfaceClass", "bInterfaceSubClass",
                   "bInterfaceProtocol"}


def rules(name):
    return [ln.strip() for ln in (RES / name).read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def test_no_rule_needs_two_different_parents():
    for name in ("60-openalloy.rules", "61-openalloy-reactive.rules"):
        for ln in rules(name):
            attrs = {m.group(2) for m in PARENT_KEYS.finditer(ln) if m.group(1) == "ATTRS"}
            assert not (attrs & DEVICE_ATTRS and attrs & INTERFACE_ATTRS), \
                f"{name}: can never match (USB device + interface attributes): {ln}"


def test_hidraw_rules_open_only_interfaces_2_and_3():
    text = "\n".join(rules("60-openalloy.rules"))
    granted = [ln for ln in text.splitlines() if 'TAG+="uaccess"' in ln]
    assert granted
    for ln in granted:
        iface = re.search(r'ID_USB_INTERFACE_NUM\}=="([^"]+)"', ln)
        assert iface, ln
        assert set(iface.group(1).split("|")) == {"02", "03"}, ln
        assert re.search(r'ID_VENDOR_ID\}=="(0951|03f0)"', ln) and re.search(r'ID_MODEL_ID\}=="(16e6|098f)"', ln)
    assert "00" not in re.findall(r'"([0-9|]+)"', text)


def test_usb_id_is_imported_before_it_is_used():
    lines = rules("60-openalloy.rules")
    first_use = next(i for i, ln in enumerate(lines) if "ENV{ID_VENDOR_ID}==" in ln)
    imp = next(i for i, ln in enumerate(lines) if 'IMPORT{builtin}="usb_id"' in ln)
    assert imp < first_use
    assert lines[0].startswith('SUBSYSTEM!="hidraw", GOTO=')     # other devices skip it all


def test_reactive_rule_matches_on_one_parent():
    for ln in rules("61-openalloy-reactive.rules"):
        if "uaccess" in ln:
            assert "idVendor" in ln and "bInterfaceNumber" not in ln
