# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""LampArray enumeration with odd replies (external 1.5.1 review, item P4).

The healthy path - the one the HP 03f0:098f fw 21.16 takes - must stay exactly as it
is: one request + one reply per lamp, nothing more."""
import logging

from openalloy.infra import lamparray as L
from openalloy.infra.simulator import SimulatedLampArray, SimulatedLampTransport

N = 89          # lamps of the simulated ABNT2 keyboard


class Scripted(SimulatedLampTransport):
    """Rewrites the lamp id of chosen replies: answers[(request, attempt)] = id."""

    def __init__(self, answers=None):
        super().__init__(SimulatedLampArray("abnt2"))
        self.answers = answers or {}
        self.asked = []
        self.lamp_reads = 0

    def set_feature(self, data):
        if data[0] == L.R_LAMP_REQ:
            self.asked.append(int.from_bytes(data[1:3], "little"))
        return super().set_feature(data)

    def get_feature(self, report_id, length):
        data = super().get_feature(report_id, length)
        if report_id == L.R_LAMP_RESP:
            self.lamp_reads += 1
            req = self.asked[-1]
            attempt = self.asked.count(req)
            lid = self.answers.get((req, attempt))
            if lid is not None:
                data = bytearray(data)
                data[1:3] = lid.to_bytes(2, "little")
                data = bytes(data)
        return data


def test_healthy_keyboard_is_read_exactly_once_per_lamp():
    t = Scripted()
    drv = L.LampArrayDriver(t, simulated=True)
    assert t.lamp_reads == N and t.asked == list(range(N))
    assert len(drv.lamps) == N and len(drv.lamp_map) == N


def test_duplicate_reply_is_recovered_by_asking_again(caplog):
    # lamp 5 first answers as lamp 4 (duplicate); asked again it answers correctly
    t = Scripted({(5, 1): 4})
    with caplog.at_level(logging.WARNING):
        drv = L.LampArrayDriver(t, simulated=True)
    assert sorted(lp.lamp_id for lp in drv.lamps) == list(range(N))
    assert len(drv.lamp_map) == N
    assert t.asked.count(5) == 2 and t.lamp_reads == N + 1      # one extra read, only for 5


def test_out_of_order_replies_still_cover_every_lamp():
    t = Scripted({(i, 1): (i + 1) % N for i in range(N)})       # each answers as its neighbour
    drv = L.LampArrayDriver(t, simulated=True)
    assert len(drv.lamps) == N and len(drv.lamp_map) == N
    assert t.lamp_reads == N                                     # nothing missing: no retries


def test_lamp_that_never_answers_leaves_a_working_driver(caplog):
    # lamp 7 always answers as lamp 6: it can never be identified
    t = Scripted({(7, 1): 6, (7, 2): 6})
    with caplog.at_level(logging.WARNING):
        drv = L.LampArrayDriver(t, simulated=True)
    assert len(drv.lamps) == N - 1
    assert any("not identified" in r.getMessage() for r in caplog.records)
    drv.stream([(10, 20, 30)] * 94)                              # still drives the rest
    shown = t.array.key_colors()
    assert sum(1 for c in shown.values() if c == (10, 20, 30)) >= N - 1


def test_reply_outside_the_range_is_still_refused():
    t = Scripted({(3, 1): 500})
    try:
        L.LampArrayDriver(t, simulated=True)
    except L.LampArrayError:
        return
    raise AssertionError("an id outside the advertised range must be refused")
