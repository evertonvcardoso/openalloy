# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Device-facing data types (model adapted from PlasmaNGenuity core/types.py)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Capabilities:
    """What a driver can do. The GUI enables pages from this."""
    live_rgb: bool = False          # per-key streaming (software effects)
    brightness: bool = False
    profiles: int = 0               # number of onboard profile slots
    onboard_animation: bool = False  # write keyframe animation to flash
    onboard_keypress: bool = False   # write/read the key-press colour bank
    layout_detect: bool = False
    key_notifications: bool = False  # Fn/profile key reports on control iface
    macros: bool = False             # not reverse engineered for this device
    key_remap: bool = False          # not reverse engineered for this device
    game_mode: bool = False          # handled by firmware (Fn+F12), no protocol


@dataclass
class DeviceInfo:
    driver: str
    name: str
    vendor_id: int
    product_id: int
    path: str
    firmware: str = "?"
    variant: Optional[str] = None
    variant_byte: Optional[int] = None
    serial: str = ""
    capabilities: Capabilities = field(default_factory=Capabilities)
    simulated: bool = False

    @property
    def usb_id(self) -> str:
        return f"{self.vendor_id:04x}:{self.product_id:04x}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["usb_id"] = self.usb_id
        return d
