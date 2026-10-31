# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Ports (interfaces) the application layer depends on.

Same role as Linuity's application/ports, but modelled on a keyboard instead
of a two-zone microphone ring, and with PlasmaNGenuity's idea of declaring
capabilities so the UI can adapt to whatever a driver supports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, List, Optional, Sequence, Tuple

from openalloy.domain.types import Capabilities, DeviceInfo

RGB = Tuple[int, int, int]
ProgressFn = Callable[[float, str], None]
NotifyFn = Callable[[str, dict], None]


class KeyboardDriver(ABC):
    """A connected RGB keyboard."""

    # Seconds after which an unchanged live frame is sent again.
    KEEPALIVE: float = 0.25

    @property
    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    def info(self) -> DeviceInfo: ...

    @abstractmethod
    def stream(self, colors: Sequence[RGB]) -> None:
        """Push one live frame (94 colours). Must be refreshed periodically."""

    @abstractmethod
    def poll(self, timeout: float = 0.0) -> None:
        """Read and dispatch any unsolicited reports."""

    @abstractmethod
    def close(self) -> None: ...

    # Optional features ---------------------------------------------------
    def get_brightness(self) -> Optional[int]:
        return None

    def set_brightness(self, level: int) -> None:
        raise NotImplementedError

    def get_profile(self) -> Optional[int]:
        return None

    def set_profile(self, profile: int) -> None:
        raise NotImplementedError

    def read_keypress_bank(self, profile: int) -> List[RGB]:
        raise NotImplementedError

    def write_keypress_bank(self, profile: int, colors: Sequence[RGB]) -> bool:
        raise NotImplementedError

    def upload_animation(self, profile: int, frames: Sequence[Sequence[RGB]],
                         progress: Optional[ProgressFn] = None) -> RGB:
        raise NotImplementedError

    def set_notify_handler(self, fn: Optional[NotifyFn]) -> None:  # noqa: B027
        pass

    def take_control(self) -> None:  # noqa: B027
        """Called before live streaming (LampArray: leave autonomous mode)."""

    def release_control(self) -> None:  # noqa: B027
        """Give the LEDs back to the firmware (LampArray: autonomous mode)."""
