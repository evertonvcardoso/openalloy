# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""HID LampArray support (HP 03f0:098f, firmware 21.16)."""
import struct
import time

import pytest

from openalloy.application.engine import Engine, EngineError
from openalloy.config.settings import PresetStore, Settings
from openalloy.domain.layout import get_layout
from openalloy.infra import lamparray as L
from openalloy.infra.drivers import open_driver
from openalloy.infra.simulator import SimulatedLampArray, SimulatedLampTransport
from openalloy.infra.transport import HIDIOCGFEATURE, HIDIOCSFEATURE, descriptor_usage_pages

# Interface 3 descriptor exactly as reported by the user's keyboard (diag.txt).
HP_IF3 = bytes.fromhex(
    "05 59 09 01 a1 01 85 01 09 02 a1 02 09 03 15 00 27 ff ff 00 00 75 10 95 01 b1 03 09 04 09 05"
    " 09 06 09 07 09 08 15 00 27 ff ff ff 7f 75 20 95 05 b1 03 c0 85 02 09 20 a1 02 09 21 15 00 27"
    " ff ff 00 00 75 10 95 01 b1 02 c0 85 03 09 22 a1 02 09 21 15 00 27 ff ff 00 00 75 10 95 01 b1"
    " 02 09 23 09 24 09 25 09 27 09 26 15 00 27 ff ff ff 7f 75 20 95 05 b1 02 09 28 09 29 09 2a 09"
    " 2b 09 2c 09 2d 15 00 26 ff 00 75 08 95 06 b1 02 c0 85 04 09 50 a1 02 09 03 09 55 15 00 25 08"
    " 75 08 95 02 b1 02 09 21 15 00 27 ff ff 00 00 75 10 95 08 b1 02 09 51 09 52 09 53 09 54 09 51"
    " 09 52 09 53 09 54 09 51 09 52 09 53 09 54 09 51 09 52 09 53 09 54 09 51 09 52 09 53 09 54 09"
    " 51 09 52 09 53 09 54 09 51 09 52 09 53 09 54 09 51 09 52 09 53 09 54 15 00 26 ff 00 75 08 95"
    " 20 b1 02 c0 85 05 09 60 a1 02 09 55 15 00 25 08 75 08 95 01 b1 02 09 61 09 62 15 00 27 ff ff"
    " 00 00 75 10 95 02 b1 02 09 51 09 52 09 53 09 54 15 00 26 ff 00 75 08 95 04 b1 02 c0 85 06 09"
    " 70 a1 02 09 71 15 00 25 01 75 08 95 01 b1 02 c0 c0".replace(" ", ""))
HP_IF2 = bytes.fromhex("0613ff0901a101150026ff007508954009028102090391020a00ffb102c0")



def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()

def feature_sizes(d):
    """Bytes per FEATURE report id (incl. id byte), parsed from a descriptor."""
    sizes, rid, rsize, rcount, i = {}, 0, 0, 0, 0
    while i < len(d):
        b = d[i]
        n = (0, 1, 2, 4)[b & 3]
        v = int.from_bytes(d[i + 1:i + 1 + n], "little")
        tag = b & 0xFC
        if tag == 0x84:
            rid = v
        elif tag == 0x74:
            rsize = v
        elif tag == 0x94:
            rcount = v
        elif tag == 0xB0:
            sizes[rid] = sizes.get(rid, 1) + rsize * rcount // 8
        i += 1 + n
    return sizes


def test_descriptor_pages():
    assert descriptor_usage_pages(HP_IF3) == [0x59]
    assert descriptor_usage_pages(HP_IF2) == [0xFF13]


def test_report_sizes_match_the_real_descriptor():
    sizes = feature_sizes(HP_IF3)
    assert sizes[L.R_ATTRS] == L.LEN_ATTRS
    assert sizes[L.R_LAMP_REQ] == len(L.pack_lamp_request(5))
    assert sizes[L.R_LAMP_RESP] == L.LEN_LAMP_RESP
    assert sizes[L.R_MULTI] == len(L.pack_multi([(1, (1, 2, 3), 4)], True))
    assert sizes[L.R_RANGE] == len(L.pack_range(0, 10, (1, 2, 3), 4))
    assert sizes[L.R_CONTROL] == len(L.pack_control(True))


def test_ioctl_numbers_match_linux_headers():
    # include/uapi/linux/hidraw.h: _IOC(_IOC_WRITE|_IOC_READ, 'H', 0x06/0x07, len)
    assert HIDIOCSFEATURE(51) == 0xC0334806
    assert HIDIOCGFEATURE(23) == 0xC0174807


def test_multi_update_layout():
    rep = L.pack_multi([(7, (10, 20, 30), 40), (300, (1, 2, 3), 4)], complete=True)
    assert rep[:3] == bytes([4, 2, 1])
    assert struct.unpack_from("<8H", rep, 3)[:2] == (7, 300)
    assert rep[19:27] == bytes([10, 20, 30, 40, 1, 2, 3, 4])
    assert L.pack_multi([(1, (0, 0, 0), 0)], complete=False)[2] == 0


def test_variant_guess():
    assert L.guess_variant([4, 0x87]) == "abnt2"
    assert L.guess_variant([4, 0x64]) == "iso"
    assert L.guess_variant([4, 5]) == "ansi"


@pytest.mark.parametrize("variant", ["ansi", "abnt2", "iso"])
def test_every_key_maps_to_its_own_lamp(variant):
    drv = L.LampArrayDriver(SimulatedLampTransport(SimulatedLampArray(variant)), simulated=True)
    layout = get_layout(variant)
    assert drv.variant == variant
    assert set(drv.lamp_map) == set(layout.indices)
    assert len(set(drv.lamp_map.values())) == len(layout.keys)
    colors = [(i, (3 * i) % 256, 255 - i) for i in range(94)]
    drv.stream(colors)
    shown = drv.transport.array.key_colors()
    assert all(shown[k] == colors[k] for k in layout.indices)


def test_control_ownership_and_uniform_frames():
    arr = SimulatedLampArray()
    drv = L.LampArrayDriver(SimulatedLampTransport(arr), simulated=True)
    assert arr.autonomous
    drv.stream([(5, 5, 5)] * 94)
    assert not arr.autonomous
    assert arr.log[-1][0] == L.R_RANGE            # one report for a uniform frame
    drv.release_control()
    assert arr.autonomous
    drv.stream([(1, 1, 1)] * 94)                 # streaming takes control back
    assert not arr.autonomous
    drv.close()
    assert arr.autonomous


def test_only_changed_lamps_are_sent():
    arr = SimulatedLampArray()
    drv = L.LampArrayDriver(SimulatedLampTransport(arr), simulated=True)
    base = [(i, i, i) for i in range(94)]
    drv.stream(base)
    n = len(arr.log)
    changed = list(base)
    changed[19] = (255, 0, 0)
    drv.stream(changed)
    sent = arr.log[n:]
    assert len(sent) == 1 and sent[0][0] == L.R_MULTI and sent[0][1] == 1


def test_software_brightness_scales_intensity():
    arr = SimulatedLampArray()
    drv = L.LampArrayDriver(SimulatedLampTransport(arr), simulated=True)
    drv.set_brightness(128)
    drv.stream([(200, 0, 0)] * 94)
    assert next(iter(arr.colors.values()))[0] == (100, 0, 0)     # 200 * 128/255


def test_open_driver_falls_back_when_vendor_channel_is_refused():
    drv = open_driver(simulate="hp")
    assert isinstance(drv, L.LampArrayDriver)
    info = drv.info()
    assert (info.usb_id, info.firmware, info.variant) == ("03f0:098f", "21.16", "abnt2")
    assert not info.capabilities.onboard_animation


def test_open_driver_prefers_vendor_when_it_works():
    from openalloy.infra.drivers import AlloyOriginsCoreDriver
    assert isinstance(open_driver(simulate=True), AlloyOriginsCoreDriver)


# --- engine on the HP keyboard ------------------------------------------------------

@pytest.fixture
def hp():
    arr = SimulatedLampArray()
    e = Engine(lambda: open_driver("hp", arr), Settings(), PresetStore(), enable_input=False)
    e.arr = arr
    e.start()
    end = time.time() + 3
    while not e.status()["connected"] and time.time() < end:
        time.sleep(0.02)
    yield e
    e.stop()


def test_engine_streams_effects_through_lamparray(hp):
    hp.apply({"effect": "static", "params": {"color": "#00ff00"}, "keys": {"19": "#ff0000"}})
    assert wait_for(lambda: hp.arr.key_colors()[19] == (255, 0, 0))
    shown = hp.arr.key_colors()
    assert shown[19] == (255, 0, 0) and shown[26] == (0, 255, 0)
    assert hp.status()["layout"] == "abnt2"


def test_engine_release_returns_leds_to_firmware(hp):
    assert wait_for(lambda: not hp.arr.autonomous)
    hp.release()
    assert hp.arr.autonomous
    hp.resume()
    assert wait_for(lambda: not hp.arr.autonomous)


def test_onboard_and_profiles_explain_why_unavailable(hp):
    for call in (lambda: hp.upload_onboard(1, {"effect": "static"}),
                 lambda: hp.set_profile(2), lambda: hp.read_onboard(1)):
        with pytest.raises(EngineError, match="LampArray"):
            call()
    st = hp.flash_status()
    assert st["supported"] is False and st["allowed"] is False


def test_live_test_works_on_lamparray(hp):
    res = hp.live_test()
    green = [k.index for k in get_layout("abnt2").keys if k.row == 0]
    assert wait_for(lambda: all(hp.arr.key_colors()[i] == (0, 255, 0) for i in green))
    hp.confirm_live_test(True)
    assert res["expected"]


def test_brightness_on_lamparray(hp):
    assert hp.set_brightness(64) == 64


def test_detach_keeps_colours_on_the_keyboard():
    arr = SimulatedLampArray()
    drv = L.LampArrayDriver(SimulatedLampTransport(arr), simulated=True)
    frame = [(255, 0, 0)] * 94
    frame[19] = (255, 255, 255)
    drv.stream(frame)
    drv.detach()
    assert arr.autonomous is False                 # host control kept after exit
    assert arr.key_colors()[19] == (255, 255, 255)
    assert not any(r[0] == L.R_CONTROL and r[1] == 1 for r in arr.log)


def test_hold_cli_parses():
    from openalloy.presentation.cli.main import build_parser
    a = build_parser().parse_args(["hold", "#ff0000", "-k", "wasd=white", "-b", "50%"])
    assert a.cmd == "hold" and a.color == "#ff0000" and a.key == ["wasd=white"]
