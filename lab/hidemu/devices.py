# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Behaviour of the emulated keyboards.

Two models, both seen by the guest kernel as one USB device with four HID
interfaces (see hid.py):

* ``VendorKeyboard`` - Kingston 0951:16e6, firmware 21.08: the vendor channel on
  interface 2 (65-byte writes, 64-byte replies) as documented in protocol.py
  (sources: OpenRGB, hxoctkl). Interface 3 is present but answers nothing
  (STALL), as no LampArray is claimed for this model.
* ``HpKeyboard`` - HP 03f0:098f, firmware 21.16: every write to interface 2 fails
  with EPROTO (what the journal of the real keyboard shows) and interface 3 is a
  HID LampArray (feature reports 1-6).

Everything received is logged (hex + decoded) and the state is observable, so a
test can check what the program *sent*, not what it believes it sent. Faults can
be injected (control API) to exercise error paths.
"""

from __future__ import annotations

import collections
import struct
import threading
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from . import hid, keytable

EPROTO, EPIPE, ENODEV, ETIMEDOUT = 71, 32, 19, 110
RGB = Tuple[int, int, int]

# --- vendor protocol constants (protocol.py, re-stated for the device side) ----------
CMD_INFO, CMD_GET_PROFILE, CMD_READ_BANK, CMD_GET_BRIGHT = 0x10, 0x15, 0x22, 0x27
CMD_READ_STORE, CMD_WRITE_GUID, CMD_WRITE_FIVE, CMD_SET_PROFILE = 0x30, 0x93, 0x94, 0x95
CMD_WRITE_CONFIG, CMD_STREAM, CMD_COMMIT_BANK = 0xA0, 0xA2, 0xA3
CMD_ANIM_BRACKET, CMD_ANIM_FRAME, CMD_SET_BRIGHT, CMD_WRITE_STORE = 0xA4, 0xA5, 0xA7, 0xB0
KNOWN_COMMANDS = {CMD_INFO, CMD_GET_PROFILE, CMD_READ_BANK, CMD_GET_BRIGHT, CMD_READ_STORE,
                  CMD_WRITE_GUID, CMD_WRITE_FIVE, CMD_SET_PROFILE, CMD_WRITE_CONFIG, CMD_STREAM,
                  CMD_COMMIT_BANK, CMD_ANIM_BRACKET, CMD_ANIM_FRAME, CMD_SET_BRIGHT,
                  CMD_WRITE_STORE}
FLASH_COMMANDS = {CMD_WRITE_GUID, CMD_WRITE_FIVE, CMD_WRITE_CONFIG, CMD_COMMIT_BANK,
                  CMD_ANIM_BRACKET, CMD_ANIM_FRAME, CMD_WRITE_STORE}
BUFLEN, CHUNK, STORE_CHUNKS = 380, 60, 13
STATUS_OK, STATUS_BAD, STATUS_UNSUPPORTED = 0xAC, 0x01, 0x00
# How long the vendor "direct mode" survives without frames before the firmware goes
# back to its onboard profile. The project only says "a few seconds" (drivers.py,
# KEEPALIVE comment); 3 s is the lab's assumption, stated in the report.
DIRECT_TIMEOUT = 3.0


def buffer_pos(idx: int) -> int:
    """Byte position of LED ``idx`` in the 380-byte GRB-planar buffer (protocol.py)."""
    return (idx // 12) * 48 + ((idx // 6) & 1) * 8 + (idx % 6)


def unpack_buffer(buf: bytes, count: int = 94) -> List[RGB]:
    out = []
    for i in range(count):
        p = buffer_pos(i)
        out.append((buf[p + 16], buf[p], buf[p + 32]))
    return out


class Device:
    """Common part: identity, log, faults, IN queues, key presses."""

    MODEL = "?"

    def __init__(self, busid: str, vid: int, pid: int, bcd: int, manufacturer: str,
                 product: str, serial: str, variant: str, lamparray: bool = False):
        self.busid = busid
        self.vid, self.pid, self.bcd = vid, pid, bcd
        self.manufacturer, self.product, self.serial = manufacturer, product, serial
        self.variant = variant
        self.interfaces = hid.standard_interfaces(lamparray)
        self.lock = threading.RLock()
        self.log: Deque[dict] = collections.deque(maxlen=50000)
        self.seq = 0
        self.faults: Dict[str, Any] = {}
        self.in_queues: Dict[int, Deque[bytes]] = {e.address: collections.deque()
                                                   for i in self.interfaces
                                                   for e in i.endpoints if e.is_in}
        self.on_in_data: Optional[Callable[[int], None]] = None    # set by the USB/IP link
        self.plugged = True
        self.resets = 0
        self.counters: Dict[str, int] = collections.Counter()
        self.created = time.monotonic()

    # --- identity -------------------------------------------------------------------
    def strings(self) -> Dict[int, str]:
        return {1: self.manufacturer, 2: self.product, 3: self.serial}

    def interface(self, number: int) -> Optional[hid.Interface]:
        for i in self.interfaces:
            if i.number == number:
                return i
        return None

    # --- log ------------------------------------------------------------------------
    def record(self, kind: str, iface: int, data: bytes, note: str = "", status: int = 0) -> None:
        with self.lock:
            self.seq += 1
            self.log.append({"seq": self.seq, "t": round(time.monotonic() - self.created, 6),
                             "kind": kind, "if": iface, "len": len(data),
                             "hex": data.hex(), "note": note, "status": status})
            self.counters[kind] += 1

    def log_since(self, seq: int, limit: int = 5000) -> List[dict]:
        with self.lock:
            return [e for e in self.log if e["seq"] > seq][:limit]

    # --- IN data ----------------------------------------------------------------------
    def queue_in(self, ep: int, data: bytes) -> None:
        with self.lock:
            self.in_queues[ep].append(bytes(data))
        if self.on_in_data:
            self.on_in_data(ep)

    def take_in(self, ep: int) -> Optional[bytes]:
        with self.lock:
            q = self.in_queues.get(ep)
            return q.popleft() if q else None

    def press_key(self, usage: int, hold: float = 0.05) -> None:
        """A key press on interface 0 (boot keyboard): reaches evdev in the guest."""
        report = bytes([0, 0, usage & 0xFF, 0, 0, 0, 0, 0])
        self.queue_in(0x81, report)
        time.sleep(hold)
        self.queue_in(0x81, bytes(8))

    # --- USB events -------------------------------------------------------------------
    def usb_reset(self) -> None:
        """Port reset / re-enumeration: the firmware restarts on its onboard profile."""
        with self.lock:
            self.resets += 1
            for q in self.in_queues.values():
                q.clear()
            self.record("USB_RESET", -1, b"", "port reset: firmware back to onboard profile")
            self.reset_state()

    def reset_state(self) -> None:
        pass

    # --- transfers (overridden) ---------------------------------------------------------
    def out_transfer(self, ep: int, data: bytes) -> int:
        return -EPIPE

    def get_report(self, iface: int, rtype: int, rid: int, length: int) -> Tuple[int, bytes]:
        return -EPIPE, b""

    def set_report(self, iface: int, rtype: int, rid: int, data: bytes) -> int:
        if iface == 0 and rtype == 2:                  # keyboard LEDs (Num/Caps Lock)
            self.record("SET_REPORT", iface, data, "keyboard LED state")
            return 0
        return -EPIPE

    def fault(self, name: str) -> Any:
        return self.faults.get(name)

    def consume_fault(self, name: str) -> bool:
        """One-shot fault (counter); True if it fires now."""
        n = self.faults.get(name)
        if not n:
            return False
        if isinstance(n, int) and n > 0:
            n -= 1
            if n:
                self.faults[name] = n
            else:
                self.faults.pop(name, None)
        return True

    # --- what the keys show -------------------------------------------------------------
    def shown(self) -> Dict[str, Any]:
        raise NotImplementedError

    def state(self) -> Dict[str, Any]:
        return {"model": self.MODEL, "busid": self.busid, "vid": "%04x" % self.vid,
                "pid": "%04x" % self.pid, "firmware": "%x.%02x" % (self.bcd >> 8, self.bcd & 0xFF),
                "variant": self.variant, "plugged": self.plugged, "resets": self.resets,
                "faults": dict(self.faults), "counters": dict(self.counters),
                "log_seq": self.seq}


class VendorKeyboard(Device):
    """Kingston 0951:16e6 (vendor channel). Also used for the HP's refusing if2."""

    MODEL = "kingston-0951-16e6"
    IN_EP = 0x83

    def __init__(self, busid: str = "1-1", variant: str = "abnt2", refuse: bool = False,
                 vid: int = 0x0951, pid: int = 0x16E6, bcd: int = 0x2108,
                 manufacturer: str = "Kingston", product: str = "HyperX Alloy Origins Core",
                 serial: str = "LAB0001"):
        super().__init__(busid, vid, pid, bcd, manufacturer, product, serial, variant)
        self.refuse = refuse
        self.variant_byte = {"ansi": 0x09, "abnt2": 0x10, "iso": 0x00}[variant]
        self.firmware = (bcd >> 8, bcd & 0xFF)
        self.reset_state()

    def reset_state(self) -> None:
        self.profile = 1
        self.brightness = 255
        self.live = bytearray(BUFLEN)
        self.last_stream: Optional[float] = None
        self.frame_times: Deque[float] = collections.deque(maxlen=2000)
        self.banks = {p: bytes(BUFLEN) for p in (1, 2, 3)}
        self.stores = {p: bytearray(STORE_CHUNKS * CHUNK) for p in (1, 2, 3)}
        self.animations: Dict[int, List[bytes]] = {p: [bytes(BUFLEN)] for p in (1, 2, 3)}
        self.configs = {p: bytes(23) for p in (1, 2, 3)}
        self.upload: Optional[dict] = None
        self.flash_writes = 0
        self.unknown: List[int] = []
        self.dangerous: List[str] = []

    # -- transfers ----------------------------------------------------------------------
    def out_transfer(self, ep: int, data: bytes) -> int:
        if ep != 0x04:
            return -EPIPE
        if self.refuse or self.fault("refuse_out"):
            self.record("OUT", 2, data, "refused (EPROTO)", -EPROTO)
            return -EPROTO
        delay = self.fault("delay_ms")
        if delay:
            time.sleep(float(delay) / 1000)
        if self.consume_fault("unplug_after_out"):
            self.record("OUT", 2, data, "fault: unplug during this transfer", -ENODEV)
            self.plugged = False
            return -ENODEV
        self.record("OUT", 2, data, self._describe(data))
        reply = self._handle(bytes(data[:64]).ljust(64, b"\0"))
        if reply is not None:
            if self.consume_fault("drop_reply") or self.fault("mute"):
                self.record("IN", 2, reply, "fault: reply dropped", 0)
            else:
                if self.consume_fault("stale_reply"):
                    stale = bytearray(64)
                    stale[0] = CMD_GET_PROFILE if reply[0] != CMD_GET_PROFILE else CMD_INFO
                    self.queue_in(self.IN_EP, bytes(stale))
                    self.record("IN", 2, bytes(stale), "fault: unexpected reply first")
                if self.consume_fault("truncate_reply"):
                    reply = reply[:8]
                self.queue_in(self.IN_EP, reply)
                self.record("IN", 2, reply, "reply")
        return 0

    def _describe(self, pkt: bytes) -> str:
        # hidraw strips report number 0: what arrives is the 64-byte report
        cmd = pkt[0] if pkt else 0
        names = {CMD_INFO: "INFO", CMD_GET_PROFILE: "GET_PROFILE", CMD_READ_BANK: "READ_BANK",
                 CMD_GET_BRIGHT: "GET_BRIGHT", CMD_READ_STORE: "READ_STORE",
                 CMD_WRITE_GUID: "WRITE_GUID", CMD_WRITE_FIVE: "WRITE_FIVE",
                 CMD_SET_PROFILE: "SET_PROFILE", CMD_WRITE_CONFIG: "WRITE_CONFIG",
                 CMD_STREAM: "STREAM", CMD_COMMIT_BANK: "COMMIT_BANK",
                 CMD_ANIM_BRACKET: "ANIM_BRACKET", CMD_ANIM_FRAME: "ANIM_FRAME",
                 CMD_SET_BRIGHT: "SET_BRIGHT", CMD_WRITE_STORE: "WRITE_STORE"}
        if len(pkt) < 4:
            return "short packet"
        return "%s idx=%d chunk=%d len=%d" % (names.get(cmd, "UNKNOWN_%02X" % cmd), pkt[1],
                                              pkt[2], pkt[3])

    def _handle(self, pkt: bytes) -> Optional[bytes]:
        cmd, index, chunk, length = pkt[0], pkt[1], pkt[2], pkt[3]
        payload = pkt[4:4 + length]
        rep = bytearray(64)
        rep[0:4] = bytes([cmd, index, chunk, length])
        if cmd in FLASH_COMMANDS:
            self.flash_writes += 1

        def ok() -> bytes:
            if self.fault("bad_status"):
                rep[3], rep[4], rep[5] = 2, 0xEC, STATUS_BAD
                return bytes(rep)
            if length >= 2:
                rep[4], rep[5] = 0xEC, STATUS_OK
            return bytes(rep)

        if cmd not in KNOWN_COMMANDS:
            self.unknown.append(cmd)
            rep[3], rep[4], rep[5] = 2, 0xEC, STATUS_UNSUPPORTED
            return bytes(rep)
        if cmd == CMD_INFO:
            rep[3] = 0x34
            rep[4], rep[5] = self.firmware
            name = self.product.encode()[:25]
            rep[8:8 + len(name)] = name
            rep[56] = self.variant_byte
            return bytes(rep)
        if cmd == CMD_GET_PROFILE:
            rep[4] = self.profile
            return bytes(rep)
        if cmd == CMD_SET_PROFILE:
            if payload[:1] and payload[0] in (1, 2, 3):
                self.profile = payload[0]
            return ok()
        if cmd == CMD_GET_BRIGHT:
            rep[4] = self.brightness
            return bytes(rep)
        if cmd == CMD_SET_BRIGHT:
            if payload:
                self.brightness = payload[0]
            return ok()
        if cmd == CMD_STREAM:
            start = index * CHUNK
            if start + length <= BUFLEN:
                self.live[start:start + length] = payload
            now = time.monotonic()
            if index == 0:
                self.frame_times.append(now)
            self.last_stream = now
            return None
        if cmd == CMD_COMMIT_BANK:
            prof = index >> 4
            if prof in (1, 2, 3):
                self.banks[prof] = bytes(self.live)
            return ok()
        if cmd == CMD_READ_BANK:
            prof, part = index >> 4, index & 0x0F
            if prof not in (1, 2, 3) or part >= 7:
                rep[3], rep[4], rep[5] = 2, 0xEC, STATUS_BAD
                return bytes(rep)
            seg = self.banks[prof][part * CHUNK:(part + 1) * CHUNK]
            rep[4:4 + len(seg)] = seg
            return bytes(rep)
        if cmd == CMD_READ_STORE:
            if index == 0 and chunk == 0 and length == 0:
                # documented hazard: a bare 0x30 crashes the firmware (re-enumeration)
                self.dangerous.append("bare READ_STORE (0x30)")
                self.plugged = False
                return None
            if index in (1, 2, 3) and 1 <= chunk <= STORE_CHUNKS:
                start = (chunk - 1) * CHUNK
                rep[4:64] = self.stores[index][start:start + CHUNK]
            return bytes(rep)
        if cmd == CMD_WRITE_STORE:
            if index in (1, 2, 3) and 1 <= chunk <= STORE_CHUNKS:
                start = (chunk - 1) * CHUNK
                self.stores[index][start:start + CHUNK] = bytes(payload).ljust(CHUNK, b"\0")
            return ok()
        if cmd in (CMD_WRITE_GUID, CMD_WRITE_FIVE):
            return ok()
        if cmd == CMD_ANIM_BRACKET:
            if payload[:1] == b"\x55":
                self.upload = {"profile": index, "count": payload[1] if len(payload) > 1 else 0,
                               "frames": {}}
            elif payload[:1] == b"\xaa" and self.upload:
                up = self.upload
                self.animations[up["profile"]] = [bytes(up["frames"].get(n, bytes(BUFLEN)))
                                                  for n in range(1, up["count"] + 1)]
                self.upload = None
            return None
        if cmd == CMD_ANIM_FRAME:
            if self.upload is not None:
                buf = self.upload["frames"].setdefault(chunk, bytearray(BUFLEN))
                part = index & 0x0F
                buf[part * CHUNK:part * CHUNK + length] = payload
            return None
        if cmd == CMD_WRITE_CONFIG:
            if index in (1, 2, 3):
                self.configs[index] = bytes(payload)
            return ok()
        return None

    def seed_store(self, profile: int, data: bytes) -> None:
        """Put known bytes in a profile store (what the firmware already holds), so a
        test can prove an upload writes back exactly what it read."""
        self.stores[profile][:] = bytes(data).ljust(STORE_CHUNKS * CHUNK, b"\0")[:STORE_CHUNKS * CHUNK]

    def profile_key(self, profile: int) -> None:
        """Fn + profile key: the firmware switches and volunteers two notifications."""
        self.profile = profile
        a = bytearray(64)
        a[0], a[3], a[4] = 0x70, 1, profile
        b = bytearray(64)
        b[0], b[3] = 0x71, 1
        for r in (a, b):
            self.queue_in(self.IN_EP, bytes(r))
            self.record("IN", 2, bytes(r), "notification (profile key)")

    # -- observation --------------------------------------------------------------------
    def direct_active(self) -> bool:
        return self.last_stream is not None and time.monotonic() - self.last_stream < DIRECT_TIMEOUT

    def fps(self, window: float = 2.0) -> float:
        now = time.monotonic()
        recent = [t for t in self.frame_times if now - t <= window]
        return round(len(recent) / window, 2)

    def shown(self) -> Dict[str, Any]:
        """Colours on the keys, by vendor buffer index (0-93). The firmware applies
        the hardware brightness on top."""
        if self.refuse:
            return {"mode": "firmware", "colors": None}
        if self.direct_active():
            k = self.brightness / 255
            cols = [tuple(round(c * k) for c in rgb) for rgb in unpack_buffer(bytes(self.live))]
            return {"mode": "direct", "colors": cols, "brightness": self.brightness}
        return {"mode": "onboard", "profile": self.profile, "colors": None,
                "brightness": self.brightness}

    def state(self) -> Dict[str, Any]:
        s = super().state()
        sh = self.shown()
        s.update({"refuse": self.refuse, "profile": self.profile, "brightness": self.brightness,
                  "mode": sh["mode"], "fps": self.fps(), "flash_writes": self.flash_writes,
                  "unknown_commands": ["%02x" % c for c in self.unknown],
                  "dangerous": list(self.dangerous),
                  "configs": {p: c.hex() for p, c in self.configs.items()},
                  "animation_frames": {p: len(a) for p, a in self.animations.items()},
                  "stores": {p: bytes(st).hex() for p, st in self.stores.items()},
                  "banks": {p: b.hex() for p, b in self.banks.items()},
                  "colors": sh["colors"]})
        return s


class LampArray:
    """HID LampArray state machine (feature reports 1-6)."""

    def __init__(self, dev: Device, variant: str, min_update_us: int = 30000):
        self.dev = dev
        self.keys = keytable.keys(variant)
        # lamp ids in row order, as the real keyboard's ordering is not known
        self.lamps = []
        for lid, k in enumerate(sorted(self.keys, key=lambda k: (k.row, k.x))):
            self.lamps.append({"id": lid, "key": k.name, "usage": k.usage,
                               "x": int(k.cx * keytable.U_MM * 1000),
                               "y": int(k.cy * keytable.U_MM * 1000)})
        self.width_um = int(keytable.WIDTH_U * keytable.U_MM * 1000)
        self.height_um = int(keytable.HEIGHT_U * keytable.U_MM * 1000)
        self.min_update_us = min_update_us
        self.reset()

    def reset(self) -> None:
        self.colors: Dict[int, Tuple[RGB, int]] = {lp["id"]: ((0, 0, 0), 0) for lp in self.lamps}
        self.pending: Dict[int, Tuple[RGB, int]] = {}
        self.autonomous = True
        self.next_id = 0
        self.updates: Deque[float] = collections.deque(maxlen=5000)
        self.too_fast = 0
        self.last_complete: Optional[float] = None

    def attributes(self) -> bytes:
        count = len(self.lamps)
        bad = self.dev.fault("bad_lamp_count")
        if bad is not None:
            count = int(bad)
        return struct.pack("<BHIIIII", 1, count, self.width_um, self.height_um, 20000, 1,
                           self.min_update_us)

    def lamp_response(self) -> bytes:
        lid = self.next_id
        if lid > 0 and self.dev.consume_fault("dup_lamp_reply"):
            lid -= 1                                    # the previous answer, again
        if self.dev.consume_fault("wrong_lamp_id"):
            lid = (lid + 7) % len(self.lamps)
        lp = self.lamps[lid % len(self.lamps)]
        rep = struct.pack("<BHIIIII", 3, lp["id"], lp["x"], lp["y"], 0, 4000, 1)
        rep += bytes([255, 255, 255, 1, 1, lp["usage"] & 0xFF])
        self.next_id = (self.next_id + 1) % len(self.lamps)    # auto-increment (spec)
        return rep

    def set(self, rid: int, data: bytes) -> int:
        if rid == 2 and len(data) >= 3:
            self.next_id = struct.unpack_from("<H", data, 1)[0]
            return 0
        if rid == 6 and len(data) >= 2:
            self.autonomous = bool(data[1])
            return 0
        if rid == 4 and len(data) >= 51:
            count, flags = data[1], data[2]
            if not 1 <= count <= 8:
                return -EPIPE
            ids = struct.unpack_from("<8H", data, 3)
            for i in range(count):
                if ids[i] >= len(self.lamps):
                    return -EPIPE
                r, g, b, inten = data[19 + 4 * i:23 + 4 * i]
                self.pending[ids[i]] = ((r, g, b), inten)
            if flags & 1:
                self._latch()
            return 0
        if rid == 5 and len(data) >= 10:
            flags, first, last = struct.unpack_from("<BHH", data, 1)
            if first > last or last >= len(self.lamps):
                return -EPIPE
            r, g, b, inten = data[6:10]
            for lid in range(first, last + 1):
                self.pending[lid] = ((r, g, b), inten)
            if flags & 1:
                self._latch()
            return 0
        return -EPIPE

    def _latch(self) -> None:
        now = time.monotonic()
        if self.last_complete is not None and (now - self.last_complete) * 1e6 < \
                self.min_update_us * 0.9:
            self.too_fast += 1
        self.last_complete = now
        self.updates.append(now)
        self.colors.update(self.pending)
        self.pending.clear()

    def shown(self) -> Dict[str, RGB]:
        """What each key shows when the host drives the lamps (intensity applied)."""
        out = {}
        for lp in self.lamps:
            (r, g, b), inten = self.colors[lp["id"]]
            out[lp["key"]] = (r, g, b) if inten else (0, 0, 0)
        return out


class HpKeyboard(Device):
    """HP 03f0:098f firmware 21.16: vendor channel refused, HID LampArray on if3."""

    MODEL = "hp-03f0-098f"

    def __init__(self, busid: str = "1-2", variant: str = "abnt2", min_update_us: int = 30000,
                 serial: str = "LAB0002"):
        super().__init__(busid, 0x03F0, 0x098F, 0x2116, "HP, Inc", "HyperX Alloy Origins Core",
                         serial, variant, lamparray=True)
        self.array = LampArray(self, variant, min_update_us)

    def reset_state(self) -> None:
        if hasattr(self, "array"):
            self.array.reset()

    def out_transfer(self, ep: int, data: bytes) -> int:
        if ep == 0x04:
            self.record("OUT", 2, data, "vendor channel refused (EPROTO)", -EPROTO)
            return -EPROTO
        return -EPIPE

    def get_report(self, iface: int, rtype: int, rid: int, length: int) -> Tuple[int, bytes]:
        if iface != 3 or rtype != 3:
            return -EPIPE, b""
        if self.fault("stall_feature"):
            self.record("GET_FEATURE", 3, bytes([rid]), "fault: STALL", -EPIPE)
            return -EPIPE, b""
        delay = self.fault("feature_delay_ms")
        if delay:
            time.sleep(float(delay) / 1000)
        with self.lock:
            if rid == 1:
                data = self.array.attributes()
            elif rid == 3:
                data = self.array.lamp_response()
            else:
                self.record("GET_FEATURE", 3, bytes([rid]), "unsupported report", -EPIPE)
                return -EPIPE, b""
            if self.consume_fault("short_feature"):
                data = data[:5]
            self.record("GET_FEATURE", 3, data, "report %d" % rid)
            return 0, data[:length]

    def set_report(self, iface: int, rtype: int, rid: int, data: bytes) -> int:
        if iface != 3 or rtype != 3:
            return super().set_report(iface, rtype, rid, data)
        if self.fault("stall_feature"):
            self.record("SET_FEATURE", 3, data, "fault: STALL", -EPIPE)
            return -EPIPE
        if self.consume_fault("unplug_after_feature"):
            self.record("SET_FEATURE", 3, data, "fault: unplug during this transfer", -ENODEV)
            self.plugged = False
            return -ENODEV
        with self.lock:
            st = self.array.set(rid, bytes(data))
            names = {2: "LampAttributesRequest", 4: "LampMultiUpdate", 5: "LampRangeUpdate",
                     6: "LampArrayControl"}
            note = names.get(rid, "report %d" % rid)
            if rid == 6 and len(data) > 1:
                note += " autonomous=%d" % data[1]
            self.record("SET_FEATURE", 3, data, note, st)
            return st

    def shown(self) -> Dict[str, Any]:
        if self.array.autonomous:
            return {"mode": "firmware", "colors": None}
        return {"mode": "host", "colors": self.array.shown()}

    def update_rate(self, window: float = 2.0) -> float:
        now = time.monotonic()
        return round(len([t for t in self.array.updates if now - t <= window]) / window, 2)

    def state(self) -> Dict[str, Any]:
        s = super().state()
        sh = self.shown()
        s.update({"mode": sh["mode"], "autonomous": self.array.autonomous,
                  "lamp_count": len(self.array.lamps), "updates_per_s": self.update_rate(),
                  "too_fast_updates": self.array.too_fast,
                  "min_update_us": self.array.min_update_us, "colors": sh["colors"]})
        return s


def make(model: str, **kw) -> Device:
    if model in ("kingston", VendorKeyboard.MODEL):
        return VendorKeyboard(**kw)
    if model in ("hp", HpKeyboard.MODEL):
        return HpKeyboard(**kw)
    raise ValueError(model)
