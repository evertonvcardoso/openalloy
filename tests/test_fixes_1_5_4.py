# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""1.5.4: defects found by the test lab (lab/, real kernel + udev + systemd in a VM)."""
import time

import pytest

from openalloy.config.settings import PresetStore, Settings


def wait_until(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class Presence:
    def __init__(self):
        self.sig = (("/dev/hidraw3", "/sys/x/0003:03F0:098F.0008"),)

    def __call__(self):
        return self.sig


@pytest.fixture
def hp(tmp_path):
    from openalloy.application.engine import Engine
    from openalloy.infra.drivers import open_driver
    from openalloy.infra.simulator import SimulatedLampArray
    arr = SimulatedLampArray()
    pres = Presence()
    e = Engine(lambda: open_driver("hp", arr), Settings(tmp_path / "c.json"),
               PresetStore(tmp_path / "p"), enable_input=False, presence=pres)
    e.arr, e.pres = arr, pres
    e.start()
    assert wait_until(lambda: e.status()["connected"])
    yield e
    e.stop()


def test_release_after_a_fixed_colour_hands_the_leds_back(hp):
    """1.5.3: in fixed mode the device is let go, so release() had no handle and the
    keyboard kept the fixed colour (lab: AutonomousMode never sent)."""
    hp.fix({"effect": "static", "params": {"color": "#0000ff"}})
    assert wait_until(lambda: hp.status()["fixed_applied"])
    assert hp.arr.autonomous is False
    hp.release()
    assert hp.arr.autonomous is True
    assert hp.status()["mode"] == "onboard"


def test_hold_release_through_the_service_too(hp):
    from openalloy.application.backend import make_handlers
    h = make_handlers(hp)
    h["fix"](lighting={"effect": "static", "params": {"color": "#00ff00"}})
    assert wait_until(lambda: hp.status()["fixed_applied"])
    h["release"]()
    assert hp.arr.autonomous is True


def test_onboard_mode_notices_a_replug(hp):
    """1.5.3: nothing is sent in onboard mode, so a replug left a dead handle and the
    status kept saying "connected" (the lab saw it with a real kernel)."""
    events = []
    hp.subscribe(lambda kind, data: events.append(kind))
    hp.release()
    time.sleep(1.3)                                   # the first look records the identity
    old = hp._dev
    hp.pres.sig = ()                                  # unplugged
    assert wait_until(lambda: "disconnected" in events, 4)
    hp.pres.sig = (("/dev/hidraw3", "/sys/x/0003:03F0:098F.0009"),)   # plugged back
    assert wait_until(lambda: hp._dev is not None and hp._dev is not old, 6)  # a fresh handle
    assert hp.status()["connected"]


def test_deleting_a_missing_preset_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from openalloy.presentation.cli.main import main
    assert main(["--simulate-hp", "preset", "delete", "no-such-preset"]) == 1
    assert "no-such-preset" in capsys.readouterr().err


def test_vendor_refusal_logged_once_and_worded_for_the_case(caplog):
    import logging

    from openalloy.infra import drivers
    from openalloy.infra.simulator import SimulatedKeyboard, SimulatedTransport
    from openalloy.infra.transport import HidNode
    node = HidNode(path="x", vendor_id=0x0951, product_id=0x16E6, interface=2, firmware="21.08")
    drivers._refusal_logged.clear()
    with caplog.at_level(logging.DEBUG, logger="openalloy.infra.drivers"):
        for _ in range(5):
            assert drivers._try_vendor(SimulatedTransport(SimulatedKeyboard(refuse_errno=71)),
                                       node, lamparray=False) is None
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1 and "no HID LampArray" in infos[0].getMessage()
