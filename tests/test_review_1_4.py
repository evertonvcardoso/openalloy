# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Regression tests for the bugs found in the 1.4.0 full code review."""
import json
import math
import os
import socket
import threading
import time

import pytest

from openalloy.application.backend import make_handlers
from openalloy.application.effects import Lighting
from openalloy.application.effects.base import COLORS, FLOAT, INT, Param
from openalloy.application.engine import Engine
from openalloy.config.settings import DEFAULTS, PresetStore, Settings, validate
from openalloy.domain.colors import led_correct
from openalloy.infra import drivers as D
from openalloy.infra.drivers import open_driver
from openalloy.infra.lamparray import LampArrayDriver
from openalloy.infra.rpc import RpcClient, RpcError, RpcServer
from openalloy.infra.simulator import SimulatedLampArray, SimulatedLampTransport
from openalloy.infra.transport import HidNode, PermissionDenied


def wait_for(cond, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def make_engine(tmp_path, presence=None, settings=None):
    arr = SimulatedLampArray()
    e = Engine(lambda: open_driver("hp", arr), settings or Settings(tmp_path / "c.json"),
               PresetStore(tmp_path / "p"), enable_input=False, presence=presence)
    e.arr = arr
    e.start()
    assert wait_for(lambda: e.status()["connected"])
    return e


@pytest.fixture
def eng(tmp_path):
    e = make_engine(tmp_path)
    yield e
    e.stop()


# --- engine ----------------------------------------------------------------------

def test_resend_keeps_preview_working(eng):
    eng.resend()
    assert len(eng.preview_frame()) == 94


def test_resend_really_sends_again(eng):
    assert wait_for(lambda: eng._frames_sent > 0)      # streaming has started (no fixed sleep)
    n = eng._frames_sent
    eng.resend()
    # the forced frame is the one that clears the flag: proof that resend() itself sent
    assert wait_for(lambda: not eng._force_send and eng._frames_sent > n, 5.0)


def test_live_test_from_fixed_mode_returns_to_fixed(eng):
    eng.fix({"effect": "static", "params": {"color": "#00ff00"}})
    assert wait_for(lambda: eng.status()["fixed_applied"])
    eng.live_test()
    assert eng.mode == "live"
    eng.confirm_live_test(False)
    assert eng.mode == "fixed"
    assert wait_for(lambda: eng.status()["fixed_applied"])
    assert eng.arr.key_colors()[30] == (0, 255, 0)


def test_live_test_from_onboard_returns_to_onboard(eng):
    eng.release()
    eng.live_test()
    eng.confirm_live_test(False)
    assert eng.mode == "onboard" and eng.arr.autonomous


def test_replug_on_the_same_hidraw_number_reapplies(tmp_path):
    sig = {"s": (("/dev/hidraw4", "/sys/x/0003:03F0:098F.0005"),)}
    e = make_engine(tmp_path, presence=lambda: sig["s"])
    try:
        e.fix({"effect": "static", "params": {"color": "#0000ff"}})
        assert wait_for(lambda: e.status()["fixed_applied"])
        e.arr.colors = {k: ((0, 0, 0), 0) for k in e.arr.colors}
        e.arr.autonomous = True
        sig["s"] = ()                           # unplugged...
        e._wake.set()
        time.sleep(1.3)
        sig["s"] = (("/dev/hidraw4", "/sys/x/0003:03F0:098F.0005"),)   # ...same node back
        e._wake.set()
        assert wait_for(lambda: e.arr.autonomous is False, 5)
        assert e.arr.key_colors()[30] == (0, 0, 255)
    finally:
        e.stop()


def test_layout_override_applies_now_and_keeps_fixed_mode(eng):
    eng.fix({"effect": "static", "params": {"color": "#ff0000"}})
    assert wait_for(lambda: eng.status()["fixed_applied"])
    make_handlers(eng)["settings_update"](layout_override="ansi")
    assert eng.status()["layout"] == "ansi"
    assert eng.mode == "fixed"
    make_handlers(eng)["settings_update"](layout_override=None)
    assert eng.status()["layout"] == "abnt2"


def test_software_brightness_survives_a_restart(tmp_path):
    e = make_engine(tmp_path)
    e.set_brightness(128)
    e.stop()
    e2 = make_engine(tmp_path)
    try:
        e2.apply({"effect": "static", "params": {"color": "#ffffff"}})
        assert wait_for(lambda: e2.arr.key_colors()[30] == (128, 128, 128))
    finally:
        e2.stop()


def test_corrupt_settings_do_not_break_the_engine(tmp_path):
    (tmp_path / "c.json").write_text(json.dumps({"fps": "abc", "mode": "xx", "led_gamma": "nan",
                                                 "lighting": [1, 2], "onboard_frames": 9999}))
    st = Settings(tmp_path / "c.json")
    assert st.get("fps") == DEFAULTS["fps"] and st.get("mode") == "live"
    assert st.get("led_gamma") == DEFAULTS["led_gamma"] and st.get("onboard_frames") == 60
    e = make_engine(tmp_path, settings=st)
    try:
        e.apply({"effect": "static", "params": {"color": "#ff0000"}})
        assert wait_for(lambda: e.arr.key_colors()[30] == (255, 0, 0))
        assert e.status()["error"] is None
    finally:
        e.stop()


def test_busy_counter_is_balanced_after_errors(eng):
    with pytest.raises(Exception):
        eng.read_onboard(1)               # LampArray: refused before touching the device
    assert eng._busy == 0


# --- settings & presets -------------------------------------------------------------

@pytest.mark.parametrize("key,value", [
    ("fps", "abc"), ("fps", 0), ("fps", True), ("led_gamma", 7), ("led_gamma", float("nan")),
    ("layout_override", "dvorak"), ("onboard_keypress", "x"), ("restore_brightness", 300),
    ("profile_bindings", [1]), ("mode", "turbo"), ("audio_source", 5),
])
def test_invalid_settings_are_rejected(tmp_path, key, value):
    st = Settings(tmp_path / "c.json")
    with pytest.raises(ValueError):
        st.update(**{key: value})
    assert st.get(key) == DEFAULTS[key]


def test_valid_settings_are_normalised():
    assert validate("fps", "24") == 24
    assert validate("reactive_input", "sim") is True
    assert validate("onboard_keypress_color", "red") == "#ff0000"
    assert validate("profile_bindings", {"1": "matrix"}) == {"1": "matrix", "2": None, "3": None}


def test_settings_update_is_all_or_nothing(tmp_path):
    st = Settings(tmp_path / "c.json")
    with pytest.raises(ValueError):
        st.update(fps=20, led_gamma=9)
    assert st.get("fps") == 30


def test_rpc_settings_rejects_bad_values(eng):
    with pytest.raises(ValueError):
        make_handlers(eng)["settings_update"](fps="rapido")


def test_broken_preset_files_are_skipped(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "array.json").write_text("[1, 2]")
    (d / "noeffect.json").write_text('{"name": "x", "lighting": {"params": {}}}')
    (d / "ok.json").write_text('{"name": "Ok", "lighting": {"effect": "static"}}')
    ids = [p["id"] for p in PresetStore(d, tmp_path / "none").list()]
    assert ids == ["ok"]


def test_preset_store_rejects_missing_names(tmp_path):
    ps = PresetStore(tmp_path / "p", tmp_path / "none")
    with pytest.raises(ValueError):
        ps.save("  ", {"effect": "static"})
    assert ps.get(None) is None and ps.delete("") is False


def test_preset_save_stores_normalised_lighting(eng):
    ident = make_handlers(eng)["preset_save"]("Meu", {"effect": "static",
                                                      "params": {"color": "red"}})
    p = eng.presets.get(ident)
    assert p["lighting"]["params"]["color"] == "#ff0000"
    with pytest.raises(ValueError):
        make_handlers(eng)["preset_save"]("Ruim", {"effect": "naoexiste"})


# --- effects & colours ----------------------------------------------------------------

@pytest.mark.parametrize("kind,value", [(INT, "inf"), (INT, [1]), (FLOAT, "nan"),
                                        (COLORS, 5), (INT, True)])
def test_param_errors_are_value_errors(kind, value):
    p = Param("x", "x", kind, 1 if kind != COLORS else ["#fff"], 0, 10, 1)
    with pytest.raises(ValueError):
        p.coerce(value)


@pytest.mark.parametrize("data", [None, [], "x", {"keys": [1], "params": [2], "reaction": 3}])
def test_lighting_from_dict_is_tolerant(data):
    light = Lighting.from_dict(data)
    assert light.params == {} and light.keys == {} and light.reaction == {}


def test_led_correct_survives_nan():
    assert led_correct([(255, 0, 0)], float("nan")) == [(255, 0, 0)]
    assert not any(math.isnan(v) for v in led_correct([(10, 20, 30)], "x")[0])


# --- driver selection -------------------------------------------------------------------

def test_lamparray_used_when_vendor_interface_is_not_accessible(monkeypatch):
    vendor = HidNode("/dev/hidraw2", 0x03F0, 0x098F, 2, firmware="21.16")
    lamp = HidNode("/dev/hidraw3", 0x03F0, 0x098F, 3, firmware="21.16", usage_pages=(0x59,))
    monkeypatch.setattr(D, "find_nodes", lambda: [(D.AlloyOriginsCoreDriver, vendor)])
    monkeypatch.setattr(D, "find_lamparray_nodes", lambda: [lamp])

    def fake_transport(path):
        if path == vendor.path:
            raise PermissionDenied(13, "sem permissao")
        return SimulatedLampTransport(SimulatedLampArray())
    monkeypatch.setattr(D, "HidrawTransport", fake_transport)
    assert isinstance(D.open_driver(), LampArrayDriver)


def test_permission_error_is_reported_when_nothing_is_accessible(monkeypatch):
    vendor = HidNode("/dev/hidraw2", 0x03F0, 0x098F, 2, firmware="21.16")
    lamp = HidNode("/dev/hidraw3", 0x03F0, 0x098F, 3, firmware="21.16", usage_pages=(0x59,))
    monkeypatch.setattr(D, "find_nodes", lambda: [(D.AlloyOriginsCoreDriver, vendor)])
    monkeypatch.setattr(D, "find_lamparray_nodes", lambda: [lamp])

    def denied(path):
        raise PermissionDenied(13, f"sem permissao em {path}")
    monkeypatch.setattr(D, "HidrawTransport", denied)
    with pytest.raises(PermissionDenied):
        D.open_driver()


# --- RPC --------------------------------------------------------------------------------

@pytest.fixture
def server(tmp_path):
    d = tmp_path / "sock"
    d.mkdir(mode=0o700)
    srv = RpcServer(d / "s.sock", {"ping": lambda: "pong", "echo": lambda x: x})
    srv.start()
    yield srv
    srv.stop()


def test_stalled_subscriber_does_not_block_the_engine(server):
    c = socket.socket(socket.AF_UNIX)
    c.connect(str(server.path))
    c.sendall(b'{"id": 0, "method": "subscribe"}\n')
    time.sleep(0.2)
    done = threading.Event()

    def spam():
        for _ in range(20000):
            server.broadcast("status", {"x": "y" * 200})
        done.set()
    threading.Thread(target=spam, daemon=True).start()
    assert done.wait(5)
    assert wait_for(lambda: server.subscriber_count() == 0, 2)   # the stalled one was dropped
    c.close()


@pytest.mark.parametrize("line", [b"[1,2]\n", b'{"method": "echo", "params": [1]}\n',
                                  b'{"method": 5}\n', b"nao e json\n"])
def test_malformed_requests_get_an_error_and_the_server_survives(server, line):
    c = socket.socket(socket.AF_UNIX)
    c.settimeout(3)
    c.connect(str(server.path))
    c.sendall(line)
    reply = json.loads(c.makefile().readline())
    assert "error" in reply
    c.close()
    assert RpcClient(server.path).call("ping") == "pong"


def test_socket_dir_must_belong_to_the_user(tmp_path, monkeypatch):
    d = tmp_path / "sock"
    d.mkdir()
    monkeypatch.setattr(os, "getuid", lambda: 4242)
    with pytest.raises(RpcError):
        RpcServer(d / "s.sock", {}).start()


def test_group_writable_socket_dir_is_tightened(tmp_path):
    d = tmp_path / "sock"
    d.mkdir()
    os.chmod(d, 0o777)
    srv = RpcServer(d / "s.sock", {"ping": lambda: "pong"})
    srv.start()
    try:
        assert os.stat(d).st_mode & 0o022 == 0
    finally:
        srv.stop()


# --- CLI ----------------------------------------------------------------------------------

def test_cli_status_names_the_fixed_mode(tmp_path, capsys, monkeypatch):
    from openalloy.application.backend import LocalBackend
    from openalloy.presentation.cli.main import build_parser, cmd_status
    be = LocalBackend(simulate="hp", settings=Settings(tmp_path / "c.json"))
    try:
        assert wait_for(lambda: be.call("status")["connected"])
        be.call("fix", lighting={"effect": "static", "params": {"color": "#ff0000"}})
        assert wait_for(lambda: be.call("status")["fixed_applied"])
        cmd_status(be, build_parser().parse_args(["status"]))
        assert "fixed color on the keyboard" in capsys.readouterr().out
    finally:
        be.close()


@pytest.mark.parametrize("argv", [["preset", "apply"], ["preset", "export", "x"],
                                  ["preset", "delete"]])
def test_cli_preset_requires_names(argv):
    from openalloy.presentation.cli.main import build_parser, cmd_preset
    with pytest.raises(SystemExit):
        cmd_preset(None, build_parser().parse_args(argv))


def test_cli_hold_reports_bad_input_without_traceback(capsys):
    from openalloy.presentation.cli.main import main
    assert main(["--simulate-hp", "hold", "#ff0000", "-b", "muito"]) == 1
    assert "Erro" in capsys.readouterr().err
