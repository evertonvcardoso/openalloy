# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
import json
import time

import pytest

from openalloy.application.backend import LocalBackend, RemoteBackend, make_handlers
from openalloy.application.engine import Engine
from openalloy.config.settings import PresetStore, Settings, socket_path
from openalloy.infra import protocol as P
from openalloy.infra.drivers import open_driver
from openalloy.infra.rpc import RpcClient, RpcError, RpcServer
from openalloy.infra.simulator import SimulatedKeyboard
from openalloy.infra.transport import DeviceNotFound


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def engine():
    kb = SimulatedKeyboard()
    drivers = []

    def opener():
        d = open_driver(True, kb)
        drivers.append(d)
        return d
    e = Engine(opener, Settings(), PresetStore(), enable_input=False)
    e.kb, e.drivers = kb, drivers
    e.start()
    assert wait_for(lambda: e.status()["connected"])
    yield e
    e.stop()


def test_engine_streams_with_keepalive(engine):
    engine.apply({"effect": "static", "params": {"color": "#0000ff"}})
    n = engine.status()["frames_sent"]
    assert wait_for(lambda: engine.status()["frames_sent"] > n + 5)
    assert P.unpack_colors(bytes(engine.kb.live))[19] == (0, 0, 255)


def test_engine_persists_lighting(engine):
    engine.apply({"effect": "spectrum", "params": {"speed": 40}})
    data = json.loads(engine.settings.path.read_text())
    assert data["lighting"]["effect"] == "spectrum" and data["lighting"]["params"]["speed"] == 40


def test_release_stops_streaming(engine):
    engine.release()
    time.sleep(0.15)
    n = engine.status()["frames_sent"]
    time.sleep(0.3)
    assert engine.status()["frames_sent"] == n


def test_engine_reconnects_after_unplug(engine):
    engine.kb.connected = False
    assert wait_for(lambda: not engine.status()["connected"], 3)
    engine.kb.connected = True
    assert wait_for(lambda: engine.status()["connected"], 5)


def test_profile_key_release_and_bind(engine):
    engine.resume()
    engine.drivers[-1].transport.inject(engine.kb.press_profile_key(2))
    assert wait_for(lambda: engine.mode == "onboard")
    engine.settings.update(profile_key_action="bind", profile_bindings={"1": "matrix"})
    engine.resume()
    engine.drivers[-1].transport.inject(engine.kb.press_profile_key(1))
    assert wait_for(lambda: engine.status()["lighting"]["effect"] == "rain")
    assert engine.mode == "live"


def test_upload_onboard_via_engine(engine):
    msgs = []
    res = engine.upload_onboard(3, {"effect": "breathing"}, frames=30, keypress="black",
                                progress=lambda f, m: msgs.append(f))
    assert res["frames"] == 30 and res["keypress_verified"] is True
    assert len(engine.kb.animations[3]) == 30
    assert engine.kb.banks[3] == bytes(380)
    assert engine.mode == "onboard" and engine.kb.profile == 3
    assert msgs and msgs[-1] == 1.0


def test_engine_without_device_reports_error():
    def opener():
        raise DeviceNotFound(19, "nada")
    e = Engine(opener, Settings(), PresetStore(), enable_input=False)
    with pytest.raises(Exception):
        e.set_brightness(10)
    assert "nada" in (e.status()["error"] or "")


def test_rpc_roundtrip_with_progress(engine):
    server = RpcServer(socket_path(), make_handlers(engine))
    engine.subscribe(server.broadcast)
    server.start()
    try:
        client = RpcClient(socket_path())
        assert client.call("ping") == "pong"
        prog = []
        res = client.call("upload_onboard", progress=lambda f, m: prog.append(f), profile=1,
                          lighting={"effect": "static", "params": {"color": "#ff0000"}})
        assert res["frames"] == 1 and prog
        with pytest.raises(RpcError):
            client.call("does_not_exist")
        with pytest.raises(RpcError):
            client.call("settings_update", evil=1)
        remote = RemoteBackend(socket_path())
        assert remote.call("status")["connected"] is True
        got = []
        remote.subscribe(lambda k, d: got.append(k))
        time.sleep(0.3)
        remote.call("release")
        assert wait_for(lambda: "mode" in got)
        remote.close()
    finally:
        server.stop()


def test_second_server_refuses(engine):
    s1 = RpcServer(socket_path(), make_handlers(engine))
    s1.start()
    try:
        with pytest.raises(RpcError):
            RpcServer(socket_path(), make_handlers(engine)).start()
    finally:
        s1.stop()


def test_local_backend_simulated():
    be = LocalBackend(simulate=True)
    try:
        assert wait_for(lambda: be.call("status")["connected"])
        assert any(p["id"] == "matrix" for p in be.call("presets_list"))
        light = be.call("preset_apply", ident="gamer-wasd")
        assert light["keys"]["19"] == "#ff1f3d"
    finally:
        be.close()
