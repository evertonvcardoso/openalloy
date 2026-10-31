# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""In-memory emulation of an Alloy Origins Core control interface.

Implements the protocol semantics documented by hxoctkl closely enough to
exercise every code path without hardware: device info, profile and
brightness get/set, live buffer streaming, key-press bank commit/read, the
780-byte store, animation upload brackets and keyframes, and unsolicited
profile-key notifications. Used by the test-suite and by ``--simulate``.
"""

from __future__ import annotations

import collections
import threading
import time
from typing import Deque, Dict, List, Optional

from openalloy.infra import protocol as P
from openalloy.infra.transport import Transport, TransportError

# Which physical layout the simulated keyboards pretend to be: ``--sim-layout``
# on the command line sets this variable (ansi = US, abnt2 = Brazil, iso = Europe).
SIM_LAYOUT_ENV = "OPENALLOY_SIM_LAYOUT"
_VARIANT_BYTE = {"ansi": 0x09, "abnt2": 0x10, "iso": 0x00}    # 0x00: not reported


def sim_layout() -> str:
    import os
    v = os.environ.get(SIM_LAYOUT_ENV, "abnt2").strip().lower()
    return v if v in _VARIANT_BYTE else "abnt2"


class SimulatedKeyboard:
    def __init__(self, variant_byte: Optional[int] = None, firmware=(0x21, 0x08),
                 refuse_errno: Optional[int] = None):
        if variant_byte is None:
            variant_byte = _VARIANT_BYTE[sim_layout()]
        # refuse_errno=71 reproduces the HP 03f0:098f fw 21.16, whose vendor
        # channel rejects every write with EPROTO.
        self.refuse_errno = refuse_errno
        self.variant_byte = variant_byte
        self.firmware = firmware
        self.profile = 1
        self.brightness = 255
        self.live = bytearray(P.BUFLEN)
        self.last_stream = 0.0
        self.banks: Dict[int, bytes] = {p: bytes(P.BUFLEN) for p in P.PROFILES}
        self.stores: Dict[int, bytearray] = {p: bytearray(P.STORE_CHUNKS * P.CHUNK)
                                             for p in P.PROFILES}
        self.animations: Dict[int, List[bytes]] = {p: [bytes(P.BUFLEN)] for p in P.PROFILES}
        self.configs: Dict[int, bytes] = {p: bytes(23) for p in P.PROFILES}
        self._upload: Optional[dict] = None
        self.log: List[bytes] = []      # every packet received (for tests)
        self.connected = True

    def handle(self, pkt: bytes) -> Optional[bytes]:
        if not self.connected:
            raise TransportError(19, "device unplugged (simulated)")
        if self.refuse_errno:
            raise TransportError(self.refuse_errno, "Protocol error (simulated)")
        self.log.append(bytes(pkt))
        data = bytearray(65)
        data[:len(pkt)] = pkt
        cmd, index, chunk, length = data[1], data[2], data[3], data[4]
        payload = bytes(data[5:5 + length])

        rep = bytearray(64)
        rep[0], rep[1], rep[2], rep[3] = cmd, index, chunk, length

        def ok():
            if length >= 2:
                rep[4], rep[5] = 0xEC, P.STATUS_OK
            return bytes(rep)

        if cmd == P.CMD_INFO:
            rep[3] = 0x34
            rep[4], rep[5] = self.firmware
            name = b"HyperX Alloy Origins Core"
            rep[8:8 + len(name)] = name
            rep[56] = self.variant_byte
            return bytes(rep)
        if cmd == P.CMD_GET_PROFILE:
            rep[4] = self.profile
            return bytes(rep)
        if cmd == P.CMD_SET_PROFILE:
            if payload and payload[0] in P.PROFILES:
                self.profile = payload[0]
            return ok()
        if cmd == P.CMD_GET_BRIGHT:
            rep[4] = self.brightness
            return bytes(rep)
        if cmd == P.CMD_SET_BRIGHT:
            if payload:
                self.brightness = payload[0]
            return ok()
        if cmd == P.CMD_STREAM:
            start = index * P.CHUNK
            self.live[start:start + length] = payload
            self.last_stream = time.monotonic()
            return None  # streaming is fire-and-forget
        if cmd == P.CMD_COMMIT_BANK:
            prof = index >> 4
            if prof in P.PROFILES:
                self.banks[prof] = bytes(self.live)
            return ok()
        if cmd == P.CMD_READ_BANK:
            prof, part = index >> 4, index & 0x0F
            if prof not in P.PROFILES or part >= P.FRAME_CHUNKS:
                rep[3] = 2
                rep[4], rep[5] = 0xEC, P.STATUS_BAD_PARAMS
                return bytes(rep)
            seg = self.banks[prof][part * P.CHUNK:(part + 1) * P.CHUNK]
            rep[4:4 + len(seg)] = seg
            return bytes(rep)
        if cmd == P.CMD_READ_STORE:
            if index == 0 and chunk == 0 and length == 0:
                # Bare 0x30 crashes real firmware into re-enumeration.
                self.connected = False
                raise TransportError(19, "firmware restarted (bare 0x30)")
            prof = index
            start = (chunk - 1) * P.CHUNK
            rep[4:64] = self.stores[prof][start:start + P.CHUNK]
            return bytes(rep)
        if cmd == P.CMD_WRITE_STORE:
            start = (chunk - 1) * P.CHUNK
            self.stores[index][start:start + P.CHUNK] = payload.ljust(P.CHUNK, b"\0")
            return ok()
        if cmd in (P.CMD_WRITE_GUID, P.CMD_WRITE_FIVE):
            return ok()
        if cmd == P.CMD_ANIM_BRACKET:
            if payload[:1] == bytes([P.ANIM_BEGIN]):
                self._upload = {"profile": index, "count": payload[1], "frames": {}}
            elif payload[:1] == bytes([P.ANIM_END]) and self._upload:
                up = self._upload
                frames = []
                for n in range(1, up["count"] + 1):
                    frames.append(bytes(up["frames"].get(n, bytearray(P.BUFLEN))))
                self.animations[up["profile"]] = frames
                self._upload = None
            return None  # 0xA4 never replies on real hardware
        if cmd == P.CMD_ANIM_FRAME:
            if self._upload is not None:
                buf = self._upload["frames"].setdefault(chunk, bytearray(P.BUFLEN))
                part = index & 0x0F
                buf[part * P.CHUNK:part * P.CHUNK + length] = payload
            return None
        if cmd == P.CMD_WRITE_CONFIG:
            self.configs[index] = payload
            return ok()
        rep[3] = 2
        rep[4], rep[5] = 0xEC, P.STATUS_UNSUPPORTED
        return bytes(rep)

    def press_profile_key(self, profile: int) -> List[bytes]:
        """What the firmware volunteers when Fn+profile key is pressed."""
        self.profile = profile
        a = bytearray(64)
        a[0], a[3], a[4] = P.NOTIFY_PROFILE_KEY, 1, profile
        b = bytearray(64)
        b[0], b[3] = P.NOTIFY_PROFILE_ACK, 1
        return [bytes(a), bytes(b)]


class SimulatedTransport(Transport):
    path = "simulado://alloy-origins-core"

    def __init__(self, keyboard: Optional[SimulatedKeyboard] = None):
        self.keyboard = keyboard or SimulatedKeyboard()
        self._queue: Deque[bytes] = collections.deque()
        self._cv = threading.Condition()
        self._open = True

    @property
    def is_open(self) -> bool:
        return self._open

    def write(self, data: bytes) -> int:
        if not self._open:
            raise TransportError(9, "device closed")
        rep = self.keyboard.handle(data)
        if rep is not None:
            with self._cv:
                self._queue.append(rep)
                self._cv.notify_all()
        return len(data)

    def inject(self, reports: List[bytes]) -> None:
        with self._cv:
            self._queue.extend(reports)
            self._cv.notify_all()

    def read(self, timeout: float) -> Optional[bytes]:
        if not self.keyboard.connected:
            raise TransportError(19, "device unplugged (simulated)")
        with self._cv:
            if not self._queue:
                self._cv.wait(timeout)
            return self._queue.popleft() if self._queue else None

    def close(self) -> None:
        self._open = False


# --- HID LampArray (interface 3 of the HP 03f0:098f, firmware 21.16) ------------

class SimulatedLampArray:
    """Implements the LampArray feature reports the HP keyboard declares."""

    def __init__(self, variant: Optional[str] = None, min_update_us: int = 0):
        import struct as _st

        from openalloy.domain.layout import HID_USAGE, get_layout
        self._st = _st
        variant = variant or sim_layout()
        layout = get_layout(variant)
        self.width_um, self.height_um = 355_000, 125_000
        self.min_update_us = min_update_us
        self.lamps = []
        for n, k in enumerate(sorted(layout.keys, key=lambda k: (k.row, k.x))):
            binding = 0 if k.name == "fn" else HID_USAGE.get(k.index, 0)
            x = int(k.u * self.width_um)
            y = int(k.v * self.height_um)
            self.lamps.append({"id": n, "x": x, "y": y, "binding": binding, "key": k.index})
        self.colors = {lp["id"]: ((0, 0, 0), 0) for lp in self.lamps}
        self.pending = {}
        self.autonomous = True
        self.requested = 0
        self.log = []
        self.connected = True

    def set_feature(self, data: bytes) -> int:
        if not self.connected:
            raise TransportError(19, "unplugged (simulated)")
        self.log.append(bytes(data))
        st, rid = self._st, data[0]
        if rid == 2:
            self.requested = st.unpack_from("<H", data, 1)[0]
        elif rid == 6:
            self.autonomous = bool(data[1])
        elif rid == 4:
            count, flags = data[1], data[2]
            ids = st.unpack_from("<8H", data, 3)
            for i in range(count):
                r, g, b, inten = data[19 + 4 * i:23 + 4 * i]
                self.pending[ids[i]] = ((r, g, b), inten)
            if flags & 1:
                self.colors.update(self.pending)
                self.pending.clear()
        elif rid == 5:
            flags, first, last = st.unpack_from("<BHH", data, 1)
            r, g, b, inten = data[6:10]
            for lid in range(first, last + 1):
                self.pending[lid] = ((r, g, b), inten)
            if flags & 1:
                self.colors.update(self.pending)
                self.pending.clear()
        else:
            raise TransportError(32, f"report {rid} not supported")
        return len(data)

    def get_feature(self, report_id: int, length: int) -> bytes:
        st = self._st
        if report_id == 1:
            return st.pack("<BHIIIII", 1, len(self.lamps), self.width_um, self.height_um,
                           20_000, 1, self.min_update_us)
        if report_id == 3:
            lp = self.lamps[self.requested]
            rep = st.pack("<BHIIIII", 3, lp["id"], lp["x"], lp["y"], 0, 4000, 1)
            rep += bytes([255, 255, 255, 1, 1, lp["binding"]])
            self.requested = (self.requested + 1) % len(self.lamps)
            return rep
        raise TransportError(32, f"report {report_id} not supported")

    def key_colors(self):
        """What each key shows (LED index -> rgb) when host-controlled."""
        return {lp["key"]: self.colors[lp["id"]][0] for lp in self.lamps}


class SimulatedLampTransport(Transport):
    path = "simulado://lamparray"

    def __init__(self, array: Optional[SimulatedLampArray] = None):
        self.array = array or SimulatedLampArray()
        self._open = True

    @property
    def is_open(self) -> bool:
        return self._open

    def write(self, data: bytes) -> int:
        raise TransportError(71, "Protocol error (the LampArray interface has no output)")

    def read(self, timeout: float) -> Optional[bytes]:
        return None

    def set_feature(self, data: bytes) -> int:
        return self.array.set_feature(data)

    def get_feature(self, report_id: int, length: int) -> bytes:
        return self.array.get_feature(report_id, length)

    def close(self) -> None:
        self._open = False
