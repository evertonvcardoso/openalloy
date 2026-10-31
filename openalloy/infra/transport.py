# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""HID transports.

``HidrawTransport`` talks to /dev/hidrawN with plain os.read/os.write, so the
core needs no hidapi/pyusb (Linuity needed both, PlasmaNGenuity needed
hidapi). Nodes are resolved through sysfs by USB id + interface number,
never by a fixed /dev/hidrawN, because the numbering moves on replug.
"""

from __future__ import annotations

import errno
import fcntl
import os
import select
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

from openalloy.i18n import N_, Message

SYSFS_HIDRAW = "/sys/class/hidraw"


class TransportError(OSError):
    pass


class DeviceNotFound(TransportError):
    pass


class PermissionDenied(TransportError):
    pass


@dataclass
class HidNode:
    path: str              # /dev/hidrawN
    vendor_id: int
    product_id: int
    interface: int
    name: str = ""
    serial: str = ""
    sysfs: str = ""
    usage_pages: tuple = ()        # from the HID report descriptor (read-only sysfs)
    firmware: str = ""             # USB bcdDevice, e.g. "21.16"


def _read(path: str) -> Optional[str]:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def enumerate_hidraw(base: str = SYSFS_HIDRAW) -> List[HidNode]:
    """List every hidraw node with its USB identity (no permissions needed)."""
    nodes: List[HidNode] = []
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return nodes
    for entry in entries:
        uevent = _read(os.path.join(base, entry, "device", "uevent")) or ""
        fields = dict(line.split("=", 1) for line in uevent.splitlines() if "=" in line)
        hid_id = fields.get("HID_ID", "")
        try:
            _bus, vid, pid = (int(p, 16) for p in hid_id.split(":"))
        except ValueError:
            continue
        dev = os.path.realpath(os.path.join(base, entry, "device"))
        iface_raw = _read(os.path.join(os.path.dirname(dev), "bInterfaceNumber"))
        try:
            iface = int(iface_raw, 16) if iface_raw is not None else -1
        except ValueError:
            iface = -1
        pages: tuple = ()
        try:
            with open(os.path.join(dev, "report_descriptor"), "rb") as fh:
                pages = tuple(descriptor_usage_pages(fh.read()))
        except OSError:
            pass
        bcd = _read(os.path.join(os.path.dirname(os.path.dirname(dev)), "bcdDevice")) or ""
        firmware = f"{bcd[:2]}.{bcd[2:]}" if len(bcd) == 4 else bcd
        nodes.append(HidNode(
            path="/dev/" + entry, vendor_id=vid, product_id=pid, interface=iface,
            name=fields.get("HID_NAME", ""), serial=fields.get("HID_UNIQ", ""),
            sysfs=dev, usage_pages=pages, firmware=firmware,
        ))
    return nodes


def descriptor_usage_pages(d: bytes) -> List[int]:
    """Usage pages declared in a HID report descriptor (short items only)."""
    pages: List[int] = []
    i = 0
    while i < len(d):
        b = d[i]
        if b == 0xFE:
            i += 3 + (d[i + 1] if i + 1 < len(d) else 0)
            continue
        size = (0, 1, 2, 4)[b & 3]
        if (b & 0xFC) == 0x04:                       # global Usage Page
            page = int.from_bytes(d[i + 1:i + 1 + size], "little")
            if page not in pages:
                pages.append(page)
        i += 1 + size
    return pages


def _ioc_rw(nr: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ord("H") << 8) | nr


def HIDIOCSFEATURE(size: int) -> int:  # noqa: N802 - kernel name
    return _ioc_rw(0x06, size)


def HIDIOCGFEATURE(size: int) -> int:  # noqa: N802 - kernel name
    return _ioc_rw(0x07, size)


class Transport(ABC):
    path: str = ""

    @abstractmethod
    def write(self, data: bytes) -> int: ...

    @abstractmethod
    def read(self, timeout: float) -> Optional[bytes]:
        """Return one input report or None on timeout."""

    @abstractmethod
    def close(self) -> None: ...

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    # Feature reports (SET_REPORT/GET_REPORT on the control endpoint).
    def set_feature(self, data: bytes) -> int:
        """data[0] is the report id."""
        raise NotImplementedError

    def get_feature(self, report_id: int, length: int) -> bytes:
        """Return the report including its id byte; ``length`` includes the id."""
        raise NotImplementedError


class HidrawTransport(Transport):
    REPORT = 64

    def __init__(self, path: str):
        self.path = path
        self._fd: Optional[int] = None
        self._lock = threading.Lock()
        self.open()

    def open(self) -> None:
        try:
            self._fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError as exc:
            raise PermissionDenied(errno.EACCES, Message(N_("no permission to open {path}"),
                path=self.path)) from exc
        except FileNotFoundError as exc:
            raise DeviceNotFound(errno.ENOENT, Message(N_("{path} does not exist"),
                path=self.path)) from exc

    @property
    def is_open(self) -> bool:
        return self._fd is not None

    def write(self, data: bytes) -> int:
        if self._fd is None:
            raise TransportError(errno.EBADF, Message(N_("device closed")))
        try:
            written = os.write(self._fd, data)
        except OSError as exc:
            raise TransportError(exc.errno, Message(N_("write to {path} failed: {error}"),
                path=self.path, error=exc.strerror or exc)) from exc
        if written != len(data):
            raise TransportError(errno.EIO, Message(N_("incomplete HID write: {written}/{total} bytes on "
                                              "{path}"), written=written, total=len(data), path=self.path))
        return written

    def read(self, timeout: float) -> Optional[bytes]:
        if self._fd is None:
            raise TransportError(errno.EBADF, Message(N_("device closed")))
        try:
            ready, _w, _x = select.select([self._fd], [], [], max(0.0, timeout))
            if not ready:
                return None
            return os.read(self._fd, self.REPORT + 1)
        except BlockingIOError:
            return None
        except OSError as exc:
            raise TransportError(exc.errno, Message(N_("read from {path} failed: {error}"),
                path=self.path, error=exc.strerror or exc)) from exc

    def set_feature(self, data: bytes) -> int:
        if self._fd is None:
            raise TransportError(errno.EBADF, Message(N_("device closed")))
        buf = bytearray(data)
        try:
            return fcntl.ioctl(self._fd, HIDIOCSFEATURE(len(buf)), buf, True)
        except OSError as exc:
            raise TransportError(exc.errno, Message(N_("sending report {report} to {path} failed: {error}"),
                report=data[0], path=self.path, error=exc.strerror or exc)) from exc

    def get_feature(self, report_id: int, length: int) -> bytes:
        if self._fd is None:
            raise TransportError(errno.EBADF, Message(N_("device closed")))
        buf = bytearray(length)
        buf[0] = report_id
        try:
            n = fcntl.ioctl(self._fd, HIDIOCGFEATURE(length), buf, True)
        except OSError as exc:
            raise TransportError(exc.errno, Message(N_("reading report {report} from {path} failed: {error}"),
                report=report_id, path=self.path, error=exc.strerror or exc)) from exc
        return bytes(buf[:n]) if isinstance(n, int) and 0 < n <= length else bytes(buf)

    def close(self) -> None:
        with self._lock:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
