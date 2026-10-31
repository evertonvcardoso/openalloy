# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Fast layer of the lab: the program's real drivers against the emulated devices,
through an adapter that behaves like the kernel's hidraw (no VM, seconds).

What the adapter reproduces from the kernel: a write to a device without report ids
drops the leading report number 0 before the OUT transfer; a failed transfer becomes
an OSError with the URB's errno; feature reports go through GET/SET_REPORT.
"""

from __future__ import annotations

import errno
import struct
import sys
import time
from pathlib import Path

import pytest

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))
sys.path.insert(0, str(LAB.parent))

from hidemu import devices, hid, keytable  # noqa: E402

from openalloy.infra import protocol as P  # noqa: E402
from openalloy.infra.drivers import AlloyOriginsCoreDriver  # noqa: E402
from openalloy.infra.lamparray import LampArrayDriver  # noqa: E402
from openalloy.infra.transport import HidNode, Transport, TransportError, descriptor_usage_pages  # noqa: E402


class HidrawLike(Transport):
    def __init__(self, dev: devices.Device, iface: int):
        self.dev, self.iface = dev, iface
        self.path = f"emu://{dev.busid}/if{iface}"
        self._open = True

    @property
    def is_open(self) -> bool:
        return self._open

    def write(self, data: bytes) -> int:
        payload = data[1:] if data[:1] == b"\0" else data
        st = self.dev.out_transfer(0x04, payload)
        if st < 0:
            raise TransportError(-st, f"write failed ({errno.errorcode.get(-st)})")
        return len(data)

    def read(self, timeout: float):
        end = time.monotonic() + timeout
        while True:
            rep = self.dev.take_in(0x83)
            if rep is not None or time.monotonic() >= end:
                return rep
            time.sleep(0.002)

    def set_feature(self, data: bytes) -> int:
        st = self.dev.set_report(self.iface, 3, data[0], data)
        if st < 0:
            raise TransportError(-st, "SET_REPORT failed")
        return len(data)

    def get_feature(self, report_id: int, length: int) -> bytes:
        st, data = self.dev.get_report(self.iface, 3, report_id, length)
        if st < 0:
            raise TransportError(-st, "GET_REPORT failed")
        return data

    def close(self) -> None:
        self._open = False


@pytest.fixture
def hp():
    return devices.HpKeyboard(variant="abnt2", min_update_us=0)


@pytest.fixture
def kingston():
    return devices.VendorKeyboard(variant="abnt2")


def lamp_driver(dev):
    node = HidNode(path="emu", vendor_id=dev.vid, product_id=dev.pid, interface=3,
                   firmware="21.16")
    return LampArrayDriver(HidrawLike(dev, 3), node)


# --- descriptors ----------------------------------------------------------------------

def test_program_reads_the_usage_pages_of_the_emulated_interfaces():
    pages = [descriptor_usage_pages(i.report_descriptor) for i in hid.standard_interfaces(True)]
    assert 0x59 in pages[3] and 0xFF13 in pages[2] and 0x07 in pages[0]


def test_lamparray_report_sizes_match_what_the_driver_packs():
    from openalloy.infra import lamparray as L
    assert hid.LAMP_REPORT_SIZES[1] == L.LEN_ATTRS
    assert hid.LAMP_REPORT_SIZES[3] == L.LEN_LAMP_RESP
    assert hid.LAMP_REPORT_SIZES[4] == len(L.pack_multi([(0, (1, 2, 3), 1)], True))
    assert hid.LAMP_REPORT_SIZES[5] == len(L.pack_range(0, 1, (1, 2, 3), 1))
    assert hid.LAMP_REPORT_SIZES[6] == len(L.pack_control(True))


# --- HP / LampArray -----------------------------------------------------------------------

def test_vendor_channel_of_the_hp_fails_with_eproto(hp):
    drv = AlloyOriginsCoreDriver(HidrawLike(hp, 2))
    with pytest.raises(OSError) as e:
        drv.info()
    assert e.value.errno == errno.EPROTO


@pytest.mark.parametrize("variant,count", [("abnt2", 89), ("ansi", 87), ("iso", 88)])
def test_lamparray_enumeration_and_mapping(variant, count):
    dev = devices.HpKeyboard(variant=variant, min_update_us=0)
    drv = lamp_driver(dev)
    assert drv.attrs.lamp_count == count
    assert len(drv.lamp_map) == count
    assert drv.variant == variant


def test_every_key_lands_on_the_lamp_of_its_usage(hp):
    from openalloy.domain.layout import HID_USAGE, get_layout
    drv = lamp_driver(hp)
    layout = get_layout("abnt2")
    colors = [(0, 0, 0)] * 94
    want = {}
    for n, k in enumerate(layout.keys):
        c = (n + 1, 200 - n, (n * 31) % 250)
        colors[k.index] = c
        want[k.index] = c
    drv.stream(colors)
    shown = hp.array.shown()
    by_usage = keytable.by_usage("abnt2")
    for idx, c in want.items():
        usage = HID_USAGE.get(idx)
        # the program's placeholder for Fn (0xE8, reserved in the HID tables) has no
        # lamp binding: the driver places it by position
        name = by_usage[usage].name if usage in by_usage else "fn"
        assert shown[name] == c, (idx, usage, name)


def test_brightness_scales_on_the_device(hp):
    drv = lamp_driver(hp)
    drv.set_brightness(128)
    drv.stream([(255, 0, 0)] * 94)
    assert set(hp.array.shown().values()) == {(128, 0, 0)}


def test_single_colour_uses_one_range_report(hp):
    drv = lamp_driver(hp)
    seq = hp.seq
    drv.stream([(0, 255, 0)] * 94)
    sent = [e for e in hp.log_since(seq) if e["kind"] == "SET_FEATURE"]
    assert [e["hex"][:2] for e in sent] == ["06", "05"]      # take control, one range


def test_duplicate_lamp_reply_is_recovered(hp):
    hp.faults["dup_lamp_reply"] = 1
    drv = lamp_driver(hp)
    assert len(drv.lamp_map) == 89


def test_release_returns_control_to_the_firmware(hp):
    drv = lamp_driver(hp)
    drv.stream([(1, 2, 3)] * 94)
    assert not hp.array.autonomous
    drv.close()
    assert hp.array.autonomous


def test_minimum_update_interval_is_respected():
    dev = devices.HpKeyboard(variant="abnt2", min_update_us=30000)
    drv = lamp_driver(dev)
    for n in range(10):
        drv.stream([(n * 20, 0, 0)] * 94)
    assert dev.array.too_fast == 0


# --- Kingston / vendor ------------------------------------------------------------------------

def vendor(dev):
    return AlloyOriginsCoreDriver(HidrawLike(dev, 2))


def test_info_firmware_and_layout(kingston):
    info = vendor(kingston).info()
    assert info.firmware == "21.08" and info.variant == "abnt2"


def test_stream_buffer_decodes_to_the_same_colours(kingston):
    drv = vendor(kingston)
    cols = [((i * 3) % 256, (i * 5) % 256, (i * 7) % 256) for i in range(94)]
    drv.stream(cols)
    assert devices.unpack_buffer(bytes(kingston.live)) == cols


def test_hardware_brightness_and_profile(kingston):
    drv = vendor(kingston)
    drv.set_brightness(64)
    drv.set_profile(3)
    assert kingston.brightness == 64 and kingston.profile == 3
    assert drv.get_brightness() == 64 and drv.get_profile() == 3


def test_upload_reads_store_first_and_writes_it_back(kingston):
    pattern = bytes((i * 11) & 0xFF for i in range(780))
    kingston.seed_store(2, pattern)
    drv = vendor(kingston)
    frames = [[(i, 0, 0)] * 94 for i in range(1, 6)]
    drv.upload_animation(2, frames)
    assert bytes(kingston.stores[2]) == pattern
    assert len(kingston.animations[2]) == 5
    assert kingston.configs[2][10] == 5
    assert not kingston.dangerous and not kingston.unknown


def test_stale_and_dropped_replies(kingston):
    drv = vendor(kingston)
    kingston.faults["stale_reply"] = 1
    assert drv.get_profile() == 1
    kingston.faults["mute"] = True
    with pytest.raises(P.ProtocolError):
        drv.set_brightness(10)


def test_keypress_bank_roundtrip(kingston):
    drv = vendor(kingston)
    cols = [(i, 255 - i, 7) for i in range(94)]
    assert drv.write_keypress_bank(1, cols)
    assert drv.read_keypress_bank(1) == cols


def test_unknown_command_answers_unsupported(kingston):
    t = HidrawLike(kingston, 2)
    t.write(P.frame(0x5A))
    rep = t.read(0.1)
    assert P.status_of(rep) == "unsupported" and kingston.unknown == [0x5A]


def test_lamparray_report_parser_matches_struct():
    dev = devices.HpKeyboard()
    rep = dev.array.attributes()
    count, w, h, d, kind, upd = struct.unpack_from("<HIIIII", rep, 1)
    assert count == 89 and upd == 30000 and w > h > 0
