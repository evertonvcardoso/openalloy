# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Flash writes are unlocked per keyboard by confirming live RGB works."""
import time

import pytest

from openalloy.application import flashgate
from openalloy.application.engine import Engine, EngineError
from openalloy.config.settings import PresetStore, Settings
from openalloy.infra.drivers import AlloyOriginsCoreDriver
from openalloy.infra.simulator import SimulatedKeyboard, SimulatedTransport


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def real_like(monkeypatch):
    """Simulator that presents itself as REAL hardware (simulated=False)."""
    monkeypatch.delenv(flashgate.ENV, raising=False)
    kb = SimulatedKeyboard(variant_byte=0x10)
    e = Engine(lambda: AlloyOriginsCoreDriver(SimulatedTransport(kb), simulated=False),
               Settings(), PresetStore(), enable_input=False)
    e.kb = kb
    e.start()
    assert wait_for(lambda: e.status()["connected"])
    yield e
    e.stop()


def test_blocked_by_default_on_real_hardware(real_like):
    assert real_like.flash_status()["allowed"] is False
    with pytest.raises(EngineError, match="blocked"):
        real_like.upload_onboard(1, {"effect": "static"})
    assert real_like.kb.animations[1] == [bytes(380)]       # nothing was written


def test_failed_live_test_keeps_it_blocked(real_like):
    real_like.live_test()
    st = real_like.confirm_live_test(False)
    assert st["allowed"] is False
    assert real_like.settings.get("flash_validated") == {}


def test_confirmed_live_test_unlocks_and_restores_lighting(real_like):
    real_like.apply({"effect": "spectrum"})
    res = real_like.live_test()
    assert res["lighting"]["effect"] == "static" and "GREEN" in res["expected"]
    assert real_like.status()["lighting"]["effect"] == "static"
    st = real_like.confirm_live_test(True)
    assert st["allowed"] is True
    assert real_like.status()["lighting"]["effect"] == "spectrum"      # restored
    out = real_like.upload_onboard(2, {"effect": "static", "params": {"color": "#00ff00"}})
    assert out["frames"] == 1 and len(real_like.kb.animations[2]) == 1


def test_validation_is_tied_to_firmware(real_like):
    real_like.live_test()
    real_like.confirm_live_test(True)
    info = dict(real_like.status()["device"], firmware="99.99")
    ok, reason = flashgate.check(info, real_like.settings.get("flash_validated"))
    assert not ok and "firmware changed" in reason


def test_environment_override(real_like, monkeypatch):
    monkeypatch.setenv(flashgate.ENV, flashgate.ENV_VALUE)
    assert real_like.flash_status()["allowed"] is True


def test_live_test_does_not_persist_pattern(real_like):
    real_like.apply({"effect": "rain"}, persist=True)
    real_like.live_test()
    assert real_like.settings.get("lighting")["effect"] == "rain"
    real_like.confirm_live_test(False)


def test_test_pattern_covers_every_key():
    from openalloy.domain.layout import get_layout
    for variant in ("ansi", "abnt2", "iso"):
        lay = get_layout(variant)
        pat = flashgate.test_pattern(lay)
        greens = [k for k, c in pat["keys"].items() if c == "#00ff00"]
        assert len(greens) == len([k for k in lay.keys if k.row == 0])
