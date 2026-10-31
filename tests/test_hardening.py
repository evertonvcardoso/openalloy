# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Safety regressions for physical keyboard operations."""

from openalloy.infra import protocol as P
from openalloy.infra.drivers import AlloyOriginsCoreDriver
from openalloy.infra.transport import Transport


class UnexpectedTransport(Transport):
    path = "unexpected"
    is_open = True

    def write(self, data):
        return len(data)

    calls = 0

    def read(self, timeout):
        self.calls += 1
        return bytes([0xEE]) + bytes(63) if self.calls == 2 else None

    def close(self):
        pass


def test_unrelated_opcode_is_not_accepted_as_reply():
    driver = AlloyOriginsCoreDriver(UnexpectedTransport())
    assert driver.request(P.CMD_GET_BRIGHT, retries=1, timeout=0.001) is None


def test_simulated_stream_still_operates(driver, sim):
    colors = [(255, 0, 0)] * 94
    driver.stream(colors)
    assert P.unpack_colors(bytes(sim.live)) == colors
