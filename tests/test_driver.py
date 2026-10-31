# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
import pytest

from openalloy.infra import protocol as P
from openalloy.infra.drivers import AlloyOriginsCoreDriver


def test_match_requires_control_interface():
    assert AlloyOriginsCoreDriver.match(0x0951, 0x16E6, 2)
    assert AlloyOriginsCoreDriver.match(0x03F0, 0x098F, 2)
    assert not AlloyOriginsCoreDriver.match(0x0951, 0x16E6, 0)
    assert not AlloyOriginsCoreDriver.match(0x0951, 0x16E1, 2)   # Pulsefire Dart


def test_info(driver):
    info = driver.info()
    assert info.firmware == "21.08" and info.variant == "abnt2" and info.capabilities.profiles == 3


def test_brightness_and_profile(driver, sim):
    driver.set_brightness(64)
    assert sim.brightness == 64 and driver.get_brightness() == 64
    driver.set_profile(3)
    assert sim.profile == 3 and driver.get_profile() == 3
    with pytest.raises(P.ProtocolError):
        driver.set_profile(4)


def test_stream_updates_live_buffer(driver, sim):
    colors = [(10, 20, 30)] * 94
    driver.stream(colors)
    assert P.unpack_colors(bytes(sim.live)) == colors


def test_keypress_bank_roundtrip(driver):
    colors = [(i, 2 * i % 256, 255 - i) for i in range(94)]
    assert driver.write_keypress_bank(2, colors) is True
    assert driver.read_keypress_bank(2) == colors


def test_upload_follows_ngenuity_sequence(driver, sim):
    frames = [[(n, 0, 0)] * 94 for n in range(1, 4)]
    sim.log.clear()
    peak = driver.upload_animation(1, frames)
    ops = [p[1] for p in sim.log]
    # read store x13, set profile, 5-byte store, write store x13, guid, begin,
    # 3 frames x 7 chunks, end, config, set profile
    expected = ([P.CMD_READ_STORE] * 13 + [P.CMD_SET_PROFILE, P.CMD_WRITE_FIVE]
                + [P.CMD_WRITE_STORE] * 13 + [P.CMD_WRITE_GUID, P.CMD_ANIM_BRACKET]
                + [P.CMD_ANIM_FRAME] * 21 + [P.CMD_ANIM_BRACKET, P.CMD_WRITE_CONFIG,
                                              P.CMD_SET_PROFILE])
    assert ops == expected
    assert sim.log[13 + 2 + 13 + 1][5:7] == bytes([P.ANIM_BEGIN, 3])
    assert len(sim.animations[1]) == 3
    assert P.unpack_colors(sim.animations[1][2])[0] == (3, 0, 0)
    assert peak == (3, 0, 0)
    assert sim.configs[1][10] == 3


def test_upload_writes_store_back_unchanged(driver, sim):
    sim.stores[2][:5] = b"\x01\x02\x03\x04\x05"
    driver.upload_animation(2, [[(0, 0, 0)] * 94])
    assert bytes(sim.stores[2][:5]) == b"\x01\x02\x03\x04\x05"


def test_upload_rejects_bad_frame_counts(driver):
    with pytest.raises(P.ProtocolError):
        driver.upload_animation(1, [])
    with pytest.raises(P.ProtocolError):
        driver.upload_animation(1, [[(0, 0, 0)] * 94] * 256)


def test_never_sends_bare_0x30(driver, sim):
    driver.info()
    driver.upload_animation(1, [[(1, 1, 1)] * 94])
    driver.read_keypress_bank(1)
    for pkt in sim.log:
        if pkt[1] == P.CMD_READ_STORE:
            assert pkt[2] != 0 and pkt[3] != 0 and pkt[4] != 0
    assert sim.connected


def test_profile_key_notification_dispatch(driver, sim):
    events = []
    driver.set_notify_handler(lambda k, d: events.append((k, d)))
    driver.transport.inject(sim.press_profile_key(2))
    driver.poll(0.1)
    assert events == [("profile_key", {"profile": 2})]


def test_notifications_do_not_break_requests(driver, sim):
    events = []
    driver.set_notify_handler(lambda k, d: events.append(k))
    driver.transport.inject(sim.press_profile_key(3))
    assert driver.get_brightness() == 255
    assert "profile_key" in events
