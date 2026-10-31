# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Driver registry and the HyperX Alloy Origins Core driver.

The registry follows PlasmaNGenuity's drivers/__init__.py: each driver class
declares ``match(vid, pid, interface)`` and discovery picks the first match.
Only exact USB ids are accepted. Writing this keyboard's 380-byte buffer to
another Alloy model (different LED count) could corrupt its onboard profile.
"""

from __future__ import annotations

import errno
import logging
import threading
import time
from typing import List, Optional, Sequence, Tuple, Type

from openalloy.application.ports import KeyboardDriver, NotifyFn, ProgressFn
from openalloy.domain.layout import NUM_LEDS, variant_from_byte
from openalloy.domain.types import Capabilities, DeviceInfo
from openalloy.i18n import N_, Message, _
from openalloy.infra import protocol as P
from openalloy.infra.transport import (
    DeviceNotFound,
    HidNode,
    HidrawTransport,
    PermissionDenied,
    Transport,
    TransportError,
    enumerate_hidraw,
)

log = logging.getLogger(__name__)
RGB = Tuple[int, int, int]


class AlloyOriginsCoreDriver(KeyboardDriver):
    NAME = "hyperx_alloy_origins_core"
    # The vendor direct mode falls back to the onboard profile a few seconds after
    # frames stop: identical frames are still resent this often.
    KEEPALIVE = 0.25

    @staticmethod
    def match(vid: int, pid: int, interface: int) -> bool:
        return (vid, pid) in P.SUPPORTED_IDS and interface == P.CONTROL_INTERFACE

    def __init__(self, transport: Transport, node: Optional[HidNode] = None,
                 simulated: bool = False):
        self._t = transport
        self._node = node
        self._simulated = simulated
        self._lock = threading.RLock()
        self._notify: Optional[NotifyFn] = None
        self._info: Optional[DeviceInfo] = None

    # --- low level -------------------------------------------------------
    @property
    def transport(self) -> Transport:
        return self._t

    def set_notify_handler(self, fn: Optional[NotifyFn]) -> None:
        self._notify = fn

    def _dispatch(self, rep: bytes) -> None:
        cmd = rep[0]
        if cmd == P.NOTIFY_PROFILE_KEY:
            event = ("profile_key", {"profile": rep[4]})
        elif cmd == P.NOTIFY_KEY:
            event = ("key", {"usage": rep[4], "pressed": rep[5] == 0x01})
        elif cmd == P.NOTIFY_PROFILE_ACK:
            return
        else:
            return
        if self._notify:
            try:
                self._notify(*event)
            except Exception:  # never let a UI callback break I/O
                log.exception("notify handler failed")

    def _drain(self) -> None:
        while True:
            rep = self._t.read(0.0)
            if rep is None:
                return
            if rep[0] in P.NOTIFICATIONS:
                self._dispatch(rep)

    def send(self, command: int, index: int = 0, chunk: int = 0, length: int = 0,
             payload: bytes = b"") -> None:
        with self._lock:
            self._t.write(P.frame(command, index, chunk, length, payload))

    def request(self, command: int, index: int = 0, chunk: int = 0, length: int = 0,
                payload: bytes = b"", retries: int = 3, timeout: float = 0.3
                ) -> Optional[bytes]:
        """Send a frame and return the reply whose echoed opcode matches."""
        with self._lock:
            for _ in range(retries):
                self._drain()
                self._t.write(P.frame(command, index, chunk, length, payload))
                deadline = time.monotonic() + timeout * 5
                while time.monotonic() < deadline:
                    rep = self._t.read(timeout)
                    if rep is None:
                        break
                    if rep[0] == command:
                        return rep
                    if rep[0] in P.NOTIFICATIONS:
                        self._dispatch(rep)
                        continue
                    # A late reply to an earlier command must never be taken as
                    # the answer to this one (hardening from 1.1.0rc1).
                    log.warning("Unexpected HID reply: expected opcode %02x, got %02x",
                                command, rep[0])
                    continue
            return None

    def _expect_ok(self, rep: Optional[bytes], what: str) -> bytes:
        st = P.status_of(rep)
        if rep is None:
            raise P.ProtocolError(Message(N_("{action}: the keyboard did not answer"), action=what))
        if st not in (None, "ok"):
            raise P.ProtocolError(f"{what}: {st}")
        return rep

    # --- KeyboardDriver --------------------------------------------------
    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            live_rgb=True, brightness=True, profiles=3, onboard_animation=True,
            onboard_keypress=True, layout_detect=True, key_notifications=True,
        )

    def info(self, refresh: bool = False) -> DeviceInfo:
        if self._info is not None and not refresh:
            return self._info
        rep = self._expect_ok(self.request(P.CMD_INFO), _("device information"))
        parsed = P.parse_info(rep)
        node = self._node
        vid, pid = (node.vendor_id, node.product_id) if node else P.SUPPORTED_IDS[0]
        self._info = DeviceInfo(
            driver=self.NAME, name=parsed["name"], vendor_id=vid, product_id=pid,
            path=self._t.path, firmware=parsed["firmware"],
            variant=variant_from_byte(parsed["variant_byte"]),
            variant_byte=parsed["variant_byte"], serial=node.serial if node else "",
            capabilities=self.capabilities, simulated=self._simulated,
        )
        return self._info

    def stream(self, colors: Sequence[RGB]) -> None:
        buf = P.pack_colors(colors)
        with self._lock:
            for pkt in P.stream_packets(buf):
                self._t.write(pkt)

    def poll(self, timeout: float = 0.0) -> None:
        with self._lock:
            rep = self._t.read(timeout)
            while rep is not None:
                if rep[0] in P.NOTIFICATIONS:
                    self._dispatch(rep)
                rep = self._t.read(0.0)

    def get_brightness(self) -> Optional[int]:
        rep = self.request(P.CMD_GET_BRIGHT)
        return rep[4] if rep is not None and len(rep) >= 5 else None

    def set_brightness(self, level: int) -> None:
        level = max(0, min(255, int(level)))
        self._expect_ok(self.request(P.CMD_SET_BRIGHT, length=1, payload=bytes([level])),
                        _("set brightness"))

    def get_profile(self) -> Optional[int]:
        rep = self.request(P.CMD_GET_PROFILE)
        return rep[4] if rep is not None and len(rep) >= 5 else None

    def set_profile(self, profile: int) -> None:
        profile = P.check_profile(profile)
        self._expect_ok(self.request(P.CMD_SET_PROFILE, length=1, payload=bytes([profile])),
                        _("select profile"))

    def read_keypress_bank(self, profile: int) -> List[RGB]:
        profile = P.check_profile(profile)
        buf = bytearray(P.BUFLEN)
        with self._lock:
            for part in range(P.FRAME_CHUNKS):
                size = min(P.CHUNK, P.BUFLEN - part * P.CHUNK)
                rep = self.request(P.CMD_READ_BANK, index=(profile << 4) | part)
                st = P.status_of(rep)
                if rep is None or st is not None:
                    raise P.ProtocolError(Message(N_("reading profile {profile} part {part}: {status}"),
                        profile=profile, part=part, status=st))
                if len(rep) < 4 + size:
                    raise P.ProtocolError(Message(N_("truncated reply reading profile {profile}, part {part}"),
                        profile=profile, part=part))
                buf[part * P.CHUNK:part * P.CHUNK + size] = rep[4:4 + size]
        return P.unpack_colors(bytes(buf))

    def write_keypress_bank(self, profile: int, colors: Sequence[RGB]) -> bool:
        """Stream into the live buffer, commit it to the bank, verify read-back."""
        profile = P.check_profile(profile)
        want = P.pack_colors(colors)
        with self._lock:
            for pkt in P.stream_packets(want):
                self._t.write(pkt)
            self._expect_ok(self.request(P.CMD_COMMIT_BANK, index=profile << 4),
                            _("write the key-press layer to profile {profile}").format(
                                profile=profile))
            got = P.pack_colors(self.read_keypress_bank(profile))
        return got == want

    def upload_animation(self, profile: int, frames: Sequence[Sequence[RGB]],
                         progress: Optional[ProgressFn] = None) -> RGB:
        """Write the base layer (keyframe animation) into onboard flash.

        Follows NGENUITY's sequence as captured by hxoctkl. The 780-byte
        store is read first and written back unchanged; if it cannot be read
        the upload is refused rather than writing invented content.
        """
        profile = P.check_profile(profile)
        count = len(frames)
        if not 1 <= count <= P.MAX_FRAMES:
            raise P.ProtocolError(Message(N_("frame count {count} outside 1-{max}"),
                count=count, max=P.MAX_FRAMES))
        report = progress or (lambda f, m: None)
        packed = [P.pack_colors(list(f) + [(0, 0, 0)] * (NUM_LEDS - len(f))) for f in frames]
        peak = max((c for f in frames for c in f), key=sum, default=(0, 0, 0))
        total = P.STORE_CHUNKS * 2 + count * P.FRAME_CHUNKS + 6
        done = 0

        def tick(msg: str):
            nonlocal done
            done += 1
            report(min(1.0, done / total), msg)

        with self._lock:
            store = bytearray()
            for chunk in range(1, P.STORE_CHUNKS + 1):
                rep = self.request(P.CMD_READ_STORE, index=profile, chunk=chunk,
                                   length=P.CHUNK)
                if rep is None or P.status_of(rep) is not None:
                    raise P.ProtocolError(
                        Message(N_("could not read the profile store; writing refused so that no "
                          "made-up data is written")))
                if len(rep) < 64:
                    raise P.ProtocolError(
                        Message(N_("truncated reply reading the profile store: write cancelled")))
                store += bytes(rep[4:64])
                tick(_("reading the profile store"))

            self.send(P.CMD_SET_PROFILE, length=1, payload=bytes([profile]))
            self.send(P.CMD_WRITE_FIVE, index=profile, length=5, payload=bytes(5))
            for chunk in range(1, P.STORE_CHUNKS + 1):
                start = (chunk - 1) * P.CHUNK
                self.send(P.CMD_WRITE_STORE, index=profile, chunk=chunk, length=P.CHUNK,
                          payload=bytes(store[start:start + P.CHUNK]))
                tick(_("rewriting the profile store"))
            self.send(P.CMD_WRITE_GUID, index=profile << 4, length=0x20, payload=bytes(16))
            self.send(P.CMD_ANIM_BRACKET, index=profile, length=2,
                      payload=bytes([P.ANIM_BEGIN, count]))
            tick(_("starting the animation upload"))
            for n, buf in enumerate(packed, start=1):
                for pkt in P.anim_frame_packets(profile, n, buf):
                    self._t.write(pkt)
                    tick(_("frame {n}/{count}").format(n=n, count=count))
            self.send(P.CMD_ANIM_BRACKET, index=profile, length=2,
                      payload=bytes([P.ANIM_END, count]))
            self.send(P.CMD_WRITE_CONFIG, index=profile, length=0x17,
                      payload=P.config_block(count, peak))
            self.send(P.CMD_SET_PROFILE, length=1, payload=bytes([profile]))
            tick(_("finishing"))
            time.sleep(0.05)
            self._drain()
        report(1.0, _("done"))
        return tuple(peak)  # type: ignore[return-value]

    def close(self) -> None:
        try:
            self._t.close()
        except Exception:
            pass


DRIVERS: List[Type[AlloyOriginsCoreDriver]] = [AlloyOriginsCoreDriver]


def find_nodes() -> List[Tuple[Type[AlloyOriginsCoreDriver], HidNode]]:
    found = []
    for node in enumerate_hidraw():
        for cls in DRIVERS:
            if cls.match(node.vendor_id, node.product_id, node.interface):
                found.append((cls, node))
                break
    return found


def find_lamparray_nodes() -> List[HidNode]:
    """Supported keyboards' interfaces that declare HID LampArray (page 0x59)."""
    from openalloy.infra.lamparray import USAGE_PAGE_LIGHTING
    return [n for n in enumerate_hidraw()
            if (n.vendor_id, n.product_id) in P.SUPPORTED_IDS
            and USAGE_PAGE_LIGHTING in n.usage_pages]


def hyperx_nodes() -> List[HidNode]:
    """All HyperX/HP hidraw nodes, for diagnostics."""
    vids = {v for v, _ in P.SUPPORTED_IDS}
    return [n for n in enumerate_hidraw() if n.vendor_id in vids]


# Errors meaning "this firmware does not accept the vendor channel" (seen on the
# HP 03f0:098f with firmware 21.16: every write fails with EPROTO).
REFUSED_ERRNOS = {errno.EPROTO, errno.EPIPE, errno.ETIMEDOUT, errno.EIO}
_vendor_refused: set = set()     # (vid, pid, firmware) known to refuse it
_refusal_logged: set = set()     # the same refusal is logged once, not on every retry


def _try_vendor(transport, node, simulated=False, lamparray: bool = True
                ) -> Optional[KeyboardDriver]:
    drv = AlloyOriginsCoreDriver(transport, node, simulated=simulated)
    try:
        drv.info()
        return drv
    except (OSError, P.ProtocolError) as exc:
        drv.close()
        code = getattr(exc, "errno", None)
        if isinstance(exc, P.ProtocolError) or code in REFUSED_ERRNOS:
            key = (node.vendor_id, node.product_id, node.firmware, lamparray) if node else None
            level = logging.DEBUG if key in _refusal_logged else logging.INFO
            if key is not None:
                _refusal_logged.add(key)
            if lamparray:
                log.log(level, "Vendor channel (interface 2) refused (%s); using HID LampArray", exc)
            else:
                log.log(level, "Vendor channel (interface 2) refused (%s); no HID LampArray "
                        "interface to fall back to", exc)
            return None
        raise


def open_driver(simulate=False, simulator=None, prefer: str = "auto") -> KeyboardDriver:
    """Open the keyboard with the best available driver.

    prefer: "auto" (vendor channel, falling back to LampArray), "vendor" or
    "lamparray". simulate: False, True/"core" (Kingston fw 21.08) or "hp"
    (HP 03f0:098f fw 21.16: vendor channel refused, LampArray available).
    """
    from openalloy.infra.lamparray import LampArrayDriver
    if simulate:
        from openalloy.infra.simulator import (
            SimulatedKeyboard,
            SimulatedLampArray,
            SimulatedLampTransport,
            SimulatedTransport,
        )
        if simulate == "hp" or isinstance(simulator, SimulatedLampArray):
            array = simulator if isinstance(simulator, SimulatedLampArray) else SimulatedLampArray()
            if prefer != "lamparray":
                drv = _try_vendor(SimulatedTransport(SimulatedKeyboard(refuse_errno=71)),
                                  None, simulated=True)
                if drv:
                    return drv
            node = HidNode(path="simulado://lamparray", vendor_id=0x03F0, product_id=0x098F,
                           interface=3, serial="SIMULADO", firmware="21.16")
            return LampArrayDriver(SimulatedLampTransport(array), node, simulated=True,
                                   name="HyperX Alloy Origins Core (HP)")
        return AlloyOriginsCoreDriver(SimulatedTransport(simulator), simulated=True)

    vendor = find_nodes()
    lamps = find_lamparray_nodes()
    if not vendor and not lamps:
        raise DeviceNotFound(19, Message(N_("HyperX Alloy Origins Core not found")))
    errors: List[Message] = []
    denied: Optional[OSError] = None
    if vendor and prefer in ("auto", "vendor"):
        cls, node = vendor[0]
        key = (node.vendor_id, node.product_id, node.firmware)
        if not (prefer == "auto" and key in _vendor_refused and lamps):
            try:
                drv = _try_vendor(HidrawTransport(node.path), node, lamparray=bool(lamps))
            except OSError as exc:
                # e.g. no permission on interface 2: LampArray may still work.
                if not (lamps and prefer == "auto"):
                    raise
                if isinstance(exc, PermissionDenied):
                    denied = exc
                errors.append(Message(N_("vendor channel: {error}"), error=exc))
                drv = None
            else:
                if drv is None:
                    _vendor_refused.add(key)
                    errors.append(Message(N_("vendor channel refused by the firmware")))
            if drv:
                return drv
    if lamps and prefer in ("auto", "lamparray"):
        node = lamps[0]
        try:
            t = HidrawTransport(node.path)
        except PermissionDenied:
            raise
        try:
            return LampArrayDriver(t, node, name=node.name or "HyperX Alloy Origins Core")
        except Exception as exc:
            t.close()
            errors.append(Message("LampArray: {error}", error=exc))
    if denied is not None:
        raise denied
    raise TransportError(errno.EPROTO, Message(
        N_("the keyboard was found but refused the commands ({details})"),
        details=tuple(errors) or (Message(N_("no compatible driver")),)))

