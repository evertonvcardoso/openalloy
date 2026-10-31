# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""LampArray value checks (from the parallel 1.4.1 HP-Driver branch, originally 1.3.2).

Difference kept on purpose: a reply carrying a *different but valid* lamp id is
accepted (the lamp is recorded under the id it reports, so the key map stays
right); only ids outside the advertised range are rejected. Refusing every
mismatch could stop a keyboard that works today from being enumerated.
"""
import pytest

from openalloy.infra import lamparray as L
from openalloy.infra.simulator import SimulatedLampArray, SimulatedLampTransport


def _replying(lamp_id_for):
    class Reply(SimulatedLampTransport):
        def get_feature(self, report_id, length):
            data = super().get_feature(report_id, length)
            if report_id == L.R_LAMP_RESP:
                data = bytearray(data)
                lid = int.from_bytes(data[1:3], "little")
                data[1:3] = lamp_id_for(lid).to_bytes(2, "little")
                return bytes(data)
            return data
    return Reply(SimulatedLampArray())


def test_rejects_lamp_reply_outside_the_advertised_range():
    with pytest.raises(L.LampArrayError, match="outside the advertised range"):
        L.LampArrayDriver(_replying(lambda lid: 500), simulated=True)


def test_accepts_valid_but_different_lamp_ids():
    # every lamp answers with the id of its neighbour (wrapping): still all known lamps
    drv = L.LampArrayDriver(_replying(lambda lid: (lid + 1) % 89), simulated=True)
    assert len(drv.lamps) == 89 and len(drv.lamp_map) == 89


@pytest.mark.parametrize("start,end", [(-1, 1), (3, 2), (0, 65536)])
def test_range_rejects_invalid_lamp_ids(start, end):
    with pytest.raises(ValueError):
        L.pack_range(start, end, (1, 2, 3), 255)


@pytest.mark.parametrize("item", [(65536, (1, 2, 3), 255), (1, (300, 2, 3), 255),
                                  (1, (1, 2, 3), -1), (1, (1, 2), 255)])
def test_multi_rejects_invalid_colors_and_ids(item):
    with pytest.raises(ValueError):
        L.pack_multi([item], True)


def test_lamp_request_rejects_negative_index():
    with pytest.raises(ValueError):
        L.pack_lamp_request(-1)


def test_rpc_call_slots_are_released_after_errors(tmp_path):
    from openalloy.infra.rpc import MAX_ACTIVE_CALLS, RpcClient, RpcError, RpcServer
    srv = RpcServer(tmp_path / "s.sock", {"boom": lambda: 1 / 0, "ping": lambda: "pong"})
    srv.start()
    try:
        c = RpcClient(srv.path)
        for _ in range(MAX_ACTIVE_CALLS + 5):
            with pytest.raises(RpcError):
                c.call("boom")
        assert c.call("ping") == "pong"
    finally:
        srv.stop()
