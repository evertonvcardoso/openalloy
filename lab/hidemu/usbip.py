# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""USB/IP device server (protocol version 1.1.1), standard library only.

The guest runs ``usbip attach -r <host> -b <busid>``; its vhci-hcd driver then
presents the emulated keyboard as a real USB device: the guest kernel enumerates
it, binds usbhid to the four interfaces, creates hidraw and evdev nodes, runs the
udev rules - exactly as with a cable. Every URB the guest submits arrives here.

Wire format (big-endian), per the kernel's Documentation/usb/usbip_protocol.rst:
  OP_REQ_DEVLIST / OP_REP_DEVLIST, OP_REQ_IMPORT / OP_REP_IMPORT, then
  USBIP_CMD_SUBMIT / RET_SUBMIT and USBIP_CMD_UNLINK / RET_UNLINK (48-byte headers).

Unplug = the server closes the connection (the guest sees a disconnect) and refuses
imports until the device is plugged again.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from typing import Dict, List, Optional, Tuple

from . import hid
from .devices import EPIPE, Device

log = logging.getLogger("hidemu.usbip")

VERSION = 0x0111
OP_REQ_DEVLIST, OP_REP_DEVLIST = 0x8005, 0x0005
OP_REQ_IMPORT, OP_REP_IMPORT = 0x8003, 0x0003
CMD_SUBMIT, CMD_UNLINK, RET_SUBMIT, RET_UNLINK = 1, 2, 3, 4
USB_SPEED_FULL = 2
ECONNRESET = 104

DEV_FMT = ">256s32sIIIHHHBBBBBB"


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        part = sock.recv(n - len(buf))
        if not part:
            raise ConnectionError("peer closed")
        buf += part
    return bytes(buf)


def device_struct(dev: Device, busnum: int, devnum: int) -> bytes:
    return struct.pack(DEV_FMT, ("/sys/devices/lab/usb%d/%s" % (busnum, dev.busid)).encode(),
                       dev.busid.encode(), busnum, devnum, USB_SPEED_FULL, dev.vid, dev.pid,
                       dev.bcd, 0, 0, 0, 1, 1, len(dev.interfaces))


class Link:
    """One imported device: a TCP connection carrying its URBs."""

    def __init__(self, server: UsbipServer, dev: Device, sock: socket.socket):
        self.server = server
        self.dev = dev
        self.sock = sock
        self.wlock = threading.Lock()
        self.pending: Dict[int, List[Tuple[int, int]]] = {}   # ep addr -> [(seqnum, length)]
        self.plock = threading.Lock()
        self.configured = False
        self.alive = True
        self.urbs = 0
        dev.on_in_data = self._in_ready

    # --- sending ---------------------------------------------------------------------
    def _send(self, data: bytes) -> None:
        with self.wlock:
            try:
                self.sock.sendall(data)
            except OSError:
                self.alive = False

    def ret_submit(self, seqnum: int, status: int, data: bytes = b"", actual: Optional[int] = None,
                   ) -> None:
        if actual is None:
            actual = len(data)
        hdr = struct.pack(">IIIII", RET_SUBMIT, seqnum, 0, 0, 0)
        hdr += struct.pack(">iiiii8x", status, actual, 0, 0, 0)
        self._send(hdr + data)

    # --- interrupt IN ------------------------------------------------------------------
    def _in_ready(self, ep: int) -> None:
        while True:
            with self.plock:
                waiting = self.pending.get(ep)
                if not waiting:
                    return
                data = self.dev.take_in(ep)
                if data is None:
                    return
                seqnum, length = waiting.pop(0)
            self.ret_submit(seqnum, 0, data[:length])

    # --- main loop ---------------------------------------------------------------------
    def run(self) -> None:
        try:
            while self.alive and self.dev.plugged:
                hdr = _recv_exact(self.sock, 48)
                command, seqnum, _devid, direction, ep = struct.unpack_from(">IIIII", hdr, 0)
                if command == CMD_SUBMIT:
                    flags, length, _sf, npk, _iv, setup = struct.unpack_from(">Iiiii8s", hdr, 20)
                    data = _recv_exact(self.sock, length) if direction == 0 and length > 0 else b""
                    self.urbs += 1
                    self._submit(seqnum, direction, ep, length, setup, data)
                elif command == CMD_UNLINK:
                    (victim,) = struct.unpack_from(">I", hdr, 20)
                    self._unlink(seqnum, victim)
                else:
                    log.warning("unknown USB/IP command %d", command)
                    break
                if not self.dev.plugged:
                    break
        except (ConnectionError, OSError):
            pass
        finally:
            self.close()

    def close(self) -> None:
        self.alive = False
        self.dev.on_in_data = None
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self.server.detached(self)

    def _unlink(self, seqnum: int, victim: int) -> None:
        found = False
        with self.plock:
            for _ep, waiting in self.pending.items():
                for i, (s, _n) in enumerate(waiting):
                    if s == victim:
                        waiting.pop(i)
                        found = True
                        break
                if found:
                    break
        hdr = struct.pack(">IIIII", RET_UNLINK, seqnum, 0, 0, 0)
        hdr += struct.pack(">i24x", -ECONNRESET if found else 0)
        self._send(hdr)

    def _submit(self, seqnum: int, direction: int, ep: int, length: int, setup: bytes,
                data: bytes) -> None:
        if ep == 0:
            status, reply = self._control(setup, data)
            self.ret_submit(seqnum, status, reply if direction == 1 else b"",
                            actual=len(reply) if direction == 1 else (len(data) if status == 0 else 0))
            return
        if direction == 1:                       # interrupt IN: park until data exists
            addr = 0x80 | ep
            with self.plock:
                self.pending.setdefault(addr, []).append((seqnum, length))
            self._in_ready(addr)
            return
        status = self.dev.out_transfer(ep, data)
        self.ret_submit(seqnum, status, b"", actual=len(data) if status == 0 else 0)

    # --- control transfers -------------------------------------------------------------
    def _control(self, setup: bytes, data: bytes) -> Tuple[int, bytes]:
        rtype, req, wvalue, windex, wlength = struct.unpack("<BBHHH", setup)
        dev = self.dev
        dtype, didx = wvalue >> 8, wvalue & 0xFF
        if rtype == 0x80 and req == 6:                               # GET_DESCRIPTOR (device)
            if dtype == hid.USB_DT_DEVICE:
                return 0, hid.device_descriptor(dev.vid, dev.pid, dev.bcd)[:wlength]
            if dtype == hid.USB_DT_CONFIG:
                return 0, hid.config_descriptor(dev.interfaces)[:wlength]
            if dtype == hid.USB_DT_STRING:
                if didx == 0:
                    return 0, hid.LANGIDS[:wlength]
                text = dev.strings().get(didx)
                if text is None:
                    return -EPIPE, b""
                return 0, hid.string_descriptor(text)[:wlength]
            return -EPIPE, b""                                       # qualifier, BOS...
        if rtype == 0x81 and req == 6:                               # GET_DESCRIPTOR (iface)
            itf = dev.interface(windex & 0xFF)
            if itf is None:
                return -EPIPE, b""
            if dtype == hid.HID_DT_REPORT:
                return 0, itf.report_descriptor[:wlength]
            if dtype == hid.HID_DT_HID:
                return 0, itf.hid_descriptor()[:wlength]
            return -EPIPE, b""
        if rtype == 0x00 and req == 9:                               # SET_CONFIGURATION
            if self.configured:
                dev.usb_reset()                                      # re-enumeration
            self.configured = wvalue == 1
            return 0, b""
        if rtype == 0x23 and req == 3 and wvalue == 4:               # port reset (stub style)
            dev.usb_reset()
            return 0, b""
        if rtype == 0x80 and req == 0:                               # GET_STATUS
            return 0, b"\x02\x00"[:wlength]                          # remote wake-up capable
        if rtype in (0x00, 0x01, 0x02) and req in (1, 3, 11):        # (CLEAR|SET)_FEATURE, SET_INTERFACE
            return 0, b""
        if rtype == 0x81 and req == 10:                              # GET_INTERFACE
            return 0, b"\x00"[:wlength]
        if rtype == 0x21:                                            # HID class OUT
            if req == 0x09:                                          # SET_REPORT
                return dev.set_report(windex & 0xFF, dtype, didx, data), b""
            if req in (0x0A, 0x0B):                                  # SET_IDLE / SET_PROTOCOL
                return 0, b""
        if rtype == 0xA1:                                            # HID class IN
            if req == 0x01:                                          # GET_REPORT
                st, rep = dev.get_report(windex & 0xFF, dtype, didx, wlength)
                return st, rep[:wlength]
            if req == 0x02:
                return 0, b"\x00"[:wlength]
            if req == 0x03:
                return 0, b"\x01"[:wlength]
        dev.record("CONTROL", windex & 0xFF, setup, "unhandled request: STALL", -EPIPE)
        return -EPIPE, b""


class UsbipServer:
    def __init__(self, devices: List[Device], host: str = "0.0.0.0", port: int = 3240):
        self.devices = {d.busid: d for d in devices}
        self.host, self.port = host, port
        self.links: Dict[str, Link] = {}
        self.lock = threading.Lock()
        self.sock: Optional[socket.socket] = None
        self.stopping = threading.Event()
        self.events: List[dict] = []
        self.t0 = time.monotonic()

    def event(self, what: str, busid: str) -> None:
        self.events.append({"t": round(time.monotonic() - self.t0, 3), "event": what,
                            "busid": busid})

    def start(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(8)
        threading.Thread(target=self._accept, name="usbip-accept", daemon=True).start()

    def stop(self) -> None:
        self.stopping.set()
        if self.sock:
            self.sock.close()
        for link in list(self.links.values()):
            link.close()

    def _accept(self) -> None:
        while not self.stopping.is_set():
            try:
                conn, _addr = self.sock.accept()
            except OSError:
                return
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn: socket.socket) -> None:
        try:
            version, code, _status = struct.unpack(">HHI", _recv_exact(conn, 8))
            if code == OP_REQ_DEVLIST:
                body = b""
                plugged = [d for d in self.devices.values() if d.plugged]
                for n, d in enumerate(plugged, 1):
                    body += device_struct(d, 1, n)
                    body += b"".join(struct.pack(">BBBx", 3, i.subclass, i.protocol)
                                     for i in d.interfaces)
                conn.sendall(struct.pack(">HHII", VERSION, OP_REP_DEVLIST, 0, len(plugged)) + body)
                conn.close()
                return
            if code == OP_REQ_IMPORT:
                busid = _recv_exact(conn, 32).split(b"\0")[0].decode()
                dev = self.devices.get(busid)
                with self.lock:
                    ok = dev is not None and dev.plugged and busid not in self.links
                    if ok:
                        link = Link(self, dev, conn)
                        self.links[busid] = link
                if not ok:
                    conn.sendall(struct.pack(">HHI", VERSION, OP_REP_IMPORT, 1))
                    conn.close()
                    return
                dev.usb_reset()                        # a fresh connection = power-on
                self.event("attached", busid)
                conn.sendall(struct.pack(">HHI", VERSION, OP_REP_IMPORT, 0)
                             + device_struct(dev, 1, list(self.devices).index(busid) + 1))
                link.run()
                return
            conn.close()
        except (ConnectionError, OSError):
            conn.close()

    def detached(self, link: Link) -> None:
        with self.lock:
            if self.links.get(link.dev.busid) is link:
                del self.links[link.dev.busid]
                self.event("detached", link.dev.busid)

    # --- plug control --------------------------------------------------------------------
    def unplug(self, busid: str) -> None:
        dev = self.devices[busid]
        dev.plugged = False
        link = self.links.get(busid)
        if link:
            link.close()
        self.event("unplugged", busid)

    def plug(self, busid: str) -> None:
        self.devices[busid].plugged = True
        self.event("plugged", busid)

    def attached(self, busid: str) -> bool:
        return busid in self.links
