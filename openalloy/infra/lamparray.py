# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""HID LampArray driver (Usage Page 0x59, "Lighting and Illumination").

LampArray is the public standard behind Windows 11 Dynamic Lighting (HID Usage
Tables, page 0x59). The HP-branded Alloy Origins Core (03f0:098f) with
firmware 21.16 exposes it on USB interface 3, while refusing the legacy vendor
channel (interface 2) that OpenRGB and hxoctkl use. Being a documented host
API it only ever touches lighting - never flash.

Report layout, exactly as declared by the keyboard's descriptor
(little-endian, packed, first byte = report id):

  1 GET  LampArrayAttributes   LampCount u16, BoundingBox W/H/D u32 (um),
                               LampArrayKind u32, MinUpdateInterval u32 (us)
  2 SET  LampAttributesRequest LampId u16
  3 GET  LampAttributesResponse LampId u16, Position X/Y/Z u32 (um),
                               UpdateLatency u32, LampPurposes u32,
                               Red/Green/Blue/Intensity level counts u8,
                               IsProgrammable u8, InputBinding u8 (key usage)
  4 SET  LampMultiUpdate       LampCount u8, Flags u8, LampId u16 x8, RGBI u8 x8
  5 SET  LampRangeUpdate       Flags u8, LampIdStart u16, LampIdEnd u16, R,G,B,I u8
  6 SET  LampArrayControl      AutonomousMode u8 (1 = firmware drives the LEDs)

Flags bit 0 = LampUpdateComplete: the device latches all pending colours.
InputBinding gives the HID keyboard usage of the key under each lamp, so the
lamp -> key map comes from the keyboard itself.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from openalloy.application.ports import KeyboardDriver
from openalloy.domain.layout import (
    INDEX_BY_HID_USAGE,
    VARIANT_ABNT2,
    VARIANT_ANSI,
    VARIANT_ISO,
    get_layout,
)
from openalloy.domain.types import Capabilities, DeviceInfo
from openalloy.i18n import N_, Message
from openalloy.infra.transport import HidNode, Transport

log = logging.getLogger(__name__)
RGB = Tuple[int, int, int]

USAGE_PAGE_LIGHTING = 0x59
R_ATTRS, R_LAMP_REQ, R_LAMP_RESP, R_MULTI, R_RANGE, R_CONTROL = 1, 2, 3, 4, 5, 6
LEN_ATTRS, LEN_LAMP_RESP = 1 + 22, 1 + 28
MULTI_SLOTS = 8
FLAG_COMPLETE = 0x01
MAX_LAMPS = 512                 # sanity bound for enumeration


class LampArrayError(Exception):
    pass


@dataclass
class ArrayAttributes:
    lamp_count: int
    width_um: int
    height_um: int
    depth_um: int
    kind: int
    min_update_us: int


@dataclass
class Lamp:
    lamp_id: int
    x_um: int
    y_um: int
    z_um: int
    latency_us: int
    purposes: int
    red_levels: int
    green_levels: int
    blue_levels: int
    intensity_levels: int
    programmable: bool
    input_binding: int


# --- pure packing / parsing ----------------------------------------------------

def parse_attributes(rep: bytes) -> ArrayAttributes:
    if len(rep) < LEN_ATTRS or rep[0] != R_ATTRS:
        raise LampArrayError(Message(N_("invalid LampArray attributes report ({size} bytes)"), size=len(rep)))
    count, w, h, d, kind, upd = struct.unpack_from("<HIIIII", rep, 1)
    return ArrayAttributes(count, w, h, d, kind, upd)


def _check_rgbi(rgb: Sequence[int], intensity: int) -> None:
    if len(rgb) != 3 or any(not 0 <= int(x) <= 255 for x in (*rgb, intensity)):
        raise ValueError("LampArray: RGBI value outside 0-255")


def pack_lamp_request(lamp_id: int) -> bytes:
    if not 0 <= lamp_id <= 0xFFFF:
        raise ValueError("lamp_id outside the u16 range")
    return struct.pack("<BH", R_LAMP_REQ, lamp_id)


def parse_lamp(rep: bytes) -> Lamp:
    if len(rep) < LEN_LAMP_RESP or rep[0] != R_LAMP_RESP:
        raise LampArrayError(Message(N_("invalid LED report ({size} bytes)"), size=len(rep)))
    lid, x, y, z, lat, purp = struct.unpack_from("<HIIIII", rep, 1)
    r, g, b, i, prog, binding = struct.unpack_from("<6B", rep, 23)
    return Lamp(lid, x, y, z, lat, purp, r, g, b, i, bool(prog), binding)


def pack_multi(lamps: Sequence[Tuple[int, RGB, int]], complete: bool) -> bytes:
    """lamps: up to 8 (lamp_id, (r, g, b), intensity)."""
    if not 1 <= len(lamps) <= MULTI_SLOTS:
        raise ValueError("1 to 8 LEDs per report")
    for lid, rgb, intensity in lamps:
        if not 0 <= lid <= 0xFFFF:
            raise ValueError("LampArray: LED id outside the u16 range")
        _check_rgbi(rgb, intensity)
    ids = [lid for lid, _c, _i in lamps] + [0] * (MULTI_SLOTS - len(lamps))
    rgbi: List[int] = []
    for _lid, (r, g, b), inten in lamps:
        rgbi += [r, g, b, inten]
    rgbi += [0] * (4 * (MULTI_SLOTS - len(lamps)))
    return (struct.pack("<BBB", R_MULTI, len(lamps), FLAG_COMPLETE if complete else 0)
            + struct.pack("<8H", *ids) + bytes(rgbi))


def pack_range(first: int, last: int, rgb: RGB, intensity: int, complete: bool = True) -> bytes:
    if not 0 <= first <= last <= 0xFFFF:
        raise ValueError("LampArray: invalid LED range")
    _check_rgbi(rgb, intensity)
    return struct.pack("<BBHH4B", R_RANGE, FLAG_COMPLETE if complete else 0, first, last,
                       rgb[0], rgb[1], rgb[2], intensity)


def pack_control(autonomous: bool) -> bytes:
    return bytes([R_CONTROL, 1 if autonomous else 0])


def guess_variant(bindings: Sequence[int]) -> str:
    present = set(bindings)
    if 0x87 in present:          # International1 = ABNT2 "/?" key
        return VARIANT_ABNT2
    if 0x64 in present:          # non-US backslash = ISO extra key
        return VARIANT_ISO
    return VARIANT_ANSI


def build_lamp_map(lamps: Sequence[Lamp], attrs: ArrayAttributes) -> Dict[int, int]:
    """LED index (our 0-93 model) -> lamp id.

    Exact for every lamp that reports an InputBinding; lamps without one
    (e.g. the Fn key, which has no HID usage) are matched to the nearest
    still-unassigned key using the reported positions.
    """
    variant = guess_variant([lp.input_binding for lp in lamps])
    layout = get_layout(variant)
    mapping: Dict[int, int] = {}
    loose: List[Lamp] = []
    for lp in lamps:
        idx = INDEX_BY_HID_USAGE.get(lp.input_binding) if lp.input_binding else None
        if idx is not None and layout.key(idx) is not None and idx not in mapping:
            mapping[idx] = lp.lamp_id
        else:
            loose.append(lp)
    if loose and attrs.width_um and attrs.height_um:
        free = [k for k in layout.keys if k.index not in mapping]
        for lp in loose:
            if not free:
                break
            u, v = lp.x_um / attrs.width_um, lp.y_um / attrs.height_um
            best = min(free, key=lambda k: (k.u - u) ** 2 + ((k.v - v) * 0.4) ** 2)
            mapping[best.index] = lp.lamp_id
            free.remove(best)
    return mapping


def _scale(value: int, levels: int) -> int:
    """Map 0-255 onto the lamp's level count. A count of 255 is how an 8-bit
    channel is described (the field is a byte), so values pass through."""
    value = max(0, min(255, int(value)))
    if levels >= 255:
        return value
    if levels <= 1:
        return 1 if value else 0
    return round(value * (levels - 1) / 255)


# --- driver -------------------------------------------------------------------------

class LampArrayDriver(KeyboardDriver):
    NAME = "hid_lamparray"
    # While the host holds control the lamps keep their colours (the "fixed" mode
    # relies on it), so an unchanged frame only needs an occasional safety refresh.
    KEEPALIVE = 2.0

    def __init__(self, transport: Transport, node: Optional[HidNode] = None,
                 simulated: bool = False, name: str = "HyperX Alloy Origins Core"):
        self._t = transport
        self._node = node
        self._simulated = simulated
        self._name = name
        self._lock = threading.RLock()
        self._brightness = 255
        self._last: Dict[int, Tuple[RGB, int]] = {}
        self._last_send = 0.0
        self._controlling = False
        self.attrs = parse_attributes(self._t.get_feature(R_ATTRS, LEN_ATTRS))
        if not 0 < self.attrs.lamp_count <= MAX_LAMPS:
            raise LampArrayError(Message(N_("invalid LED count: {count}"), count=self.attrs.lamp_count))
        lamps: Dict[int, Lamp] = {}
        for i in range(self.attrs.lamp_count):
            lp = self._read_lamp(i)
            lamps.setdefault(lp.lamp_id, lp)
        # A duplicated reply hides a lamp. Ask once more for each missing id; a
        # healthy keyboard (every reply matches) never gets here.
        missing = [i for i in range(self.attrs.lamp_count) if i not in lamps]
        for i in missing:
            lp = self._read_lamp(i)
            lamps.setdefault(lp.lamp_id, lp)
        still = [i for i in range(self.attrs.lamp_count) if i not in lamps]
        if still:
            log.warning("LampArray: %d LED(s) not identified (%s); they stay unmapped",
                        len(still), ", ".join(map(str, still[:12])))
        self.lamps = [lamps[k] for k in sorted(lamps)]
        self.lamp_map = build_lamp_map(self.lamps, self.attrs)
        self.variant = guess_variant([lp.input_binding for lp in self.lamps])
        self._lamp_by_id = {lp.lamp_id: lp for lp in self.lamps}
        log.info("LampArray: %d LEDs, %d mapped to keys, layout %s, minimum interval %d us",
                 self.attrs.lamp_count, len(self.lamp_map), self.variant,
                 self.attrs.min_update_us)

    @property
    def transport(self) -> Transport:
        return self._t

    def _read_lamp(self, lamp_id: int) -> Lamp:
        self._t.set_feature(pack_lamp_request(lamp_id))
        lamp = parse_lamp(self._t.get_feature(R_LAMP_RESP, LEN_LAMP_RESP))
        if lamp.lamp_id != lamp_id:
            log.warning("LampArray: asked for LED %d, got %d", lamp_id, lamp.lamp_id)
            if lamp.lamp_id >= self.attrs.lamp_count:
                raise LampArrayError(Message(N_("LED {led} outside the advertised range"), led=lamp.lamp_id))
        return lamp

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(live_rgb=True, brightness=True, profiles=0,
                            onboard_animation=False, onboard_keypress=False,
                            layout_detect=True, key_notifications=False)

    def info(self, refresh: bool = False) -> DeviceInfo:
        node = self._node
        from openalloy.infra import protocol as P
        vid, pid = (node.vendor_id, node.product_id) if node else P.SUPPORTED_IDS[1]
        return DeviceInfo(
            driver=self.NAME, name=self._name, vendor_id=vid, product_id=pid,
            path=self._t.path, firmware=(node.firmware if node and node.firmware else "?"),
            variant=self.variant, variant_byte=None, serial=node.serial if node else "",
            capabilities=self.capabilities, simulated=self._simulated,
        )

    # --- control ownership ---------------------------------------------------
    def take_control(self) -> None:
        with self._lock:
            if not self._controlling:
                self._t.set_feature(pack_control(False))
                self._controlling = True
                self._last.clear()

    def release_control(self) -> None:
        """Hand the LEDs back to the firmware's own lighting."""
        with self._lock:
            try:
                self._t.set_feature(pack_control(True))
            finally:
                self._controlling = False
                self._last.clear()

    # --- frames ----------------------------------------------------------------
    def _lamp_value(self, lamp_id: int, c: RGB) -> Tuple[RGB, int]:
        lp = self._lamp_by_id[lamp_id]
        # Brightness is applied to the colour itself: many lamps report a single
        # intensity level (no separate dimming), so intensity stays at maximum.
        k = self._brightness / 255.0
        rgb = (_scale(round(c[0] * k), lp.red_levels), _scale(round(c[1] * k), lp.green_levels),
               _scale(round(c[2] * k), lp.blue_levels))
        return rgb, _scale(255, lp.intensity_levels)

    def stream(self, colors: Sequence[RGB]) -> None:
        with self._lock:
            if not self._controlling:
                self.take_control()
            wanted: Dict[int, Tuple[RGB, int]] = {}
            for idx, lamp_id in self.lamp_map.items():
                if idx < len(colors):
                    wanted[lamp_id] = self._lamp_value(lamp_id, colors[idx])
            # Lamps without a key (if any) are kept dark.
            filler = ((0, 0, 0), 0)
            for lp in self.lamps:
                wanted.setdefault(lp.lamp_id, filler)

            # Respect the device's minimum update interval.
            gap = self.attrs.min_update_us / 1e6 - (time.monotonic() - self._last_send)
            if gap > 0:
                time.sleep(min(gap, 0.1))

            values = set(wanted.values())
            if len(values) == 1:
                rgb, inten = values.pop()
                self._t.set_feature(pack_range(0, self.attrs.lamp_count - 1, rgb, inten))
            else:
                changed = [(lid, v) for lid, v in sorted(wanted.items()) if self._last.get(lid) != v]
                if not changed:          # refresh: resend everything (keep-alive)
                    changed = sorted(wanted.items())
                for start in range(0, len(changed), MULTI_SLOTS):
                    chunk = changed[start:start + MULTI_SLOTS]
                    last = start + MULTI_SLOTS >= len(changed)
                    self._t.set_feature(pack_multi(
                        [(lid, rgb, inten) for lid, (rgb, inten) in chunk], last))
            self._last = wanted
            self._last_send = time.monotonic()

    def poll(self, timeout: float = 0.0) -> None:
        return None

    def get_brightness(self) -> Optional[int]:
        return self._brightness

    def set_brightness(self, level: int) -> None:
        """No hardware brightness on LampArray: scales every lamp's intensity."""
        with self._lock:
            self._brightness = max(0, min(255, int(level)))
            self._last.clear()

    def detach(self) -> None:
        """Close WITHOUT handing the LEDs back: the last colours stay on the keyboard
        (for as long as the firmware keeps them - until replug/reboot/suspend at most)."""
        with self._lock:                 # never cut a frame in half
            self._controlling = False
            try:
                self._t.close()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            try:
                if self._controlling:
                    self.release_control()
            except Exception:
                pass
            try:
                self._t.close()
            except Exception:
                pass
