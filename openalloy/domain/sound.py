# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""What is playing right now, normalised to 0..1 (filled by infra.audio)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

BANDS = 12
SILENCE = 0.02


@dataclass
class Sound:
    on: bool = False
    level: float = 0.0
    beat: float = 0.0
    bands: List[float] = field(default_factory=lambda: [0.0] * BANDS)

    @property
    def live(self) -> bool:
        """Capture running *and* something audible (silence looks frozen)."""
        if not self.on:
            return False
        return self.level > SILENCE or self.beat > SILENCE or any(b > SILENCE for b in self.bands)

    def to_dict(self) -> dict:
        return {"on": self.on, "level": round(self.level, 3), "beat": round(self.beat, 3),
                "bands": [round(b, 3) for b in self.bands]}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Sound:
        if not d:
            return cls()
        bands = list(d.get("bands") or [])[:BANDS]
        return cls(bool(d.get("on")), float(d.get("level", 0)), float(d.get("beat", 0)),
                   bands + [0.0] * (BANDS - len(bands)))


