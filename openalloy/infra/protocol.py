# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""HyperX Alloy Origins Core vendor protocol - pure data, no I/O.

Sources (facts re-implemented, see THANKS):
  * OpenRGB HyperXAlloyOriginsCoreController (GPL-2.0): Direct mode 0xA2,
    brightness 0xA7, info 0x10 (variant at byte 56), GRB colour packing.
  * hxoctkl docs/PROTOCOL.md (GPL-2.0-or-later): onboard profile store,
    animation upload 0xA4/0xA5, config block 0xA0, key-press bank 0x22/0xA3,
    profile select 0x95, notifications 0x70/0x71/0x73.

Transport: USB interface 2, usage page 0xFF13, 64-byte reports, no report
IDs. Write buffers are 65 bytes; byte 0 is the report number hidraw consumes.

    request:  [0]=0  [1]=cmd  [2]=index  [3]=chunk  [4]=length  [5..]=payload
    reply:    [0]=cmd [1]=index [2]=chunk [3]=length-echo [4..]=payload
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from openalloy.domain.layout import NUM_LEDS
from openalloy.i18n import N_, Message

# --- USB identity --------------------------------------------------------
VID_KINGSTON = 0x0951
PID_ORIGINS_CORE = 0x16E6
VID_HP = 0x03F0
PID_ORIGINS_CORE_HP = 0x098F
SUPPORTED_IDS = ((VID_KINGSTON, PID_ORIGINS_CORE), (VID_HP, PID_ORIGINS_CORE_HP))
CONTROL_INTERFACE = 2
CONTROL_USAGE_PAGE = 0xFF13

PKT = 65
BUFLEN = 380
CHUNK = 60
FRAME_CHUNKS = 7          # 6 x 60 + 1 x 20 bytes
STORE_CHUNKS = 13         # 780-byte per-profile store
MAX_FRAMES = 255
PROFILES = (1, 2, 3)

# --- Opcodes -------------------------------------------------------------
CMD_INFO = 0x10
CMD_GET_PROFILE = 0x15
CMD_READ_BANK = 0x22       # read key-press colour bank
CMD_GET_BRIGHT = 0x27
CMD_READ_STORE = 0x30      # NEVER send bare: faults firmware into re-enumeration
CMD_WRITE_GUID = 0x93
CMD_WRITE_FIVE = 0x94
CMD_SET_PROFILE = 0x95
CMD_WRITE_CONFIG = 0xA0
CMD_STREAM = 0xA2
CMD_COMMIT_BANK = 0xA3
CMD_ANIM_BRACKET = 0xA4
CMD_ANIM_FRAME = 0xA5
CMD_SET_BRIGHT = 0xA7
CMD_WRITE_STORE = 0xB0

ANIM_BEGIN = 0x55
ANIM_END = 0xAA

NOTIFY_PROFILE_KEY = 0x70
NOTIFY_PROFILE_ACK = 0x71
NOTIFY_KEY = 0x73
NOTIFICATIONS = frozenset({NOTIFY_PROFILE_KEY, NOTIFY_PROFILE_ACK, NOTIFY_KEY})

STATUS_UNSUPPORTED = 0x00
STATUS_BAD_PARAMS = 0x01
STATUS_OK = 0xAC

# Firmware brightness has five effective steps over 0-255.
BRIGHTNESS_STEPS = (0, 64, 128, 192, 255)

RGB = Tuple[int, int, int]


class ProtocolError(Exception):
    pass


def frame(command: int, index: int = 0, chunk: int = 0, length: int = 0,
          payload: bytes = b"") -> bytes:
    if len(payload) > PKT - 5:
        raise ValueError("payload too large")
    pkt = bytearray(PKT)
    pkt[1] = command & 0xFF
    pkt[2] = index & 0xFF
    pkt[3] = chunk & 0xFF
    pkt[4] = length & 0xFF
    pkt[5:5 + len(payload)] = payload
    return bytes(pkt)


def buffer_pos(idx: int) -> int:
    return (idx // 12) * 48 + ((idx // 6) & 1) * 8 + (idx % 6)


def pack_colors(colors: Sequence[Sequence[int]]) -> bytes:
    """94 (r, g, b) -> 380-byte GRB-planar buffer."""
    buf = bytearray(BUFLEN)
    for idx, c in enumerate(list(colors)[:NUM_LEDS]):
        pos = buffer_pos(idx)
        buf[pos] = int(c[1]) & 0xFF
        buf[pos + 16] = int(c[0]) & 0xFF
        buf[pos + 32] = int(c[2]) & 0xFF
    return bytes(buf)


def unpack_colors(buf: bytes) -> List[RGB]:
    out = []
    for idx in range(NUM_LEDS):
        pos = buffer_pos(idx)
        out.append((buf[pos + 16], buf[pos], buf[pos + 32]))
    return out


def chunks(buf: bytes) -> List[bytes]:
    return [buf[i:i + CHUNK] for i in range(0, len(buf), CHUNK)]


def stream_packets(buf: bytes) -> List[bytes]:
    """Live (Direct-mode) packets. Each is payload + 5 bytes, as OpenRGB does."""
    out = []
    for n, part in enumerate(chunks(buf)):
        out.append(frame(CMD_STREAM, n, 0, len(part), part)[:len(part) + 5])
    return out


def anim_frame_packets(profile: int, frame_no: int, buf: bytes) -> List[bytes]:
    return [frame(CMD_ANIM_FRAME, (profile << 4) | n, frame_no, len(part), part)
            for n, part in enumerate(chunks(buf))]


def config_block(frame_count: int, peak: RGB) -> bytes:
    """Per-profile config written by NGENUITY after an animation upload."""
    cfg = bytearray(23)
    cfg[0], cfg[1] = 0x09, 0x18
    cfg[10] = frame_count & 0xFF
    cfg[12] = 0x04
    cfg[15] = 0x01
    cfg[16], cfg[17], cfg[18] = (int(peak[0]) & 0xFF, int(peak[1]) & 0xFF,
                                 int(peak[2]) & 0xFF)
    return bytes(cfg)


def status_of(rep: Optional[bytes]) -> Optional[str]:
    """None means 'data reply' (or a status we cannot see), else a status name."""
    if rep is None:
        return "silent"
    length = rep[3]
    body = rep[4:4 + length] if length else b""
    if len(body) >= 2 and body[0] == 0xEC:
        return {STATUS_UNSUPPORTED: "unsupported", STATUS_BAD_PARAMS: "bad-parameters",
                STATUS_OK: "ok"}.get(body[1], "ec%02x" % body[1])
    return None


def parse_info(rep: bytes) -> dict:
    name = bytes(rep[8:33]).split(b"\x00")[0].decode("ascii", "replace").strip()
    return {
        "firmware": "%02X.%02X" % (rep[4], rep[5]),
        "name": name or "HyperX Alloy Origins Core",
        "variant_byte": rep[56] if len(rep) > 56 else None,
    }


def check_profile(profile: int) -> int:
    profile = int(profile)
    if profile not in PROFILES:
        raise ProtocolError(Message(N_("the profile must be 1, 2 or 3")))
    return profile

