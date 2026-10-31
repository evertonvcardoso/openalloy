# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Gate for persistent (flash) writes to the keyboard.

Origin: OpenAlloy 1.1.0rc1 blocked onboard writes on real hardware unless the
daemon was started with OPENALLOY_ENABLE_FLASH=I_ACCEPT_THE_RISK. That is safe
but impractical (editing a systemd unit). Here the gate opens per keyboard,
once the user has confirmed that *live* RGB really works on that unit:

    1. ``live_test`` shows a known pattern (left red, right blue, F-row green);
    2. the user confirms what the physical keys show;
    3. the unit (USB id + serial + firmware) is recorded as validated.

If live colours do not work - as reported with OpenRGB on a HP 03f0:098f -
the protocol is not understood by that firmware, and writing to its flash
would be a blind write; the gate stays closed. A firmware change requires
validating again. The environment variable remains as an explicit override.
"""

from __future__ import annotations

import os
import time
from typing import Optional, Tuple

from openalloy.i18n import N_, _, format_datetime, parse_datetime

ENV = "OPENALLOY_ENABLE_FLASH"
ENV_VALUE = "I_ACCEPT_THE_RISK"


def device_key(info: Optional[dict]) -> Optional[str]:
    if not info:
        return None
    return f"{info.get('usb_id', '?')}:{info.get('serial') or '-'}:{info.get('driver', '?')}"


def check(info: Optional[dict], validated: dict) -> Tuple[bool, str]:
    """Return (allowed, reason) for flash writes to this keyboard."""
    if not info:
        return False, _("keyboard not connected")
    if info.get("simulated"):
        return True, _("simulated keyboard")
    if os.environ.get(ENV) == ENV_VALUE:
        return True, _("unlocked by the {variable} variable").format(variable=ENV)
    rec = (validated if isinstance(validated, dict) else {}).get(device_key(info))
    if not isinstance(rec, dict) or not rec:          # absent or corrupted record
        return False, _("first confirm that live RGB works on this keyboard - the 'Test RGB' "
                        "button or 'openalloy test-rgb'. That way nothing is written to the "
                        "flash of a firmware that does not understand the commands")
    if rec.get("firmware") != info.get("firmware"):
        return False, (_("the firmware changed ({old} -> {new}); test live RGB again").format(
            old=rec.get("firmware"), new=info.get("firmware")))
    when = parse_datetime(rec.get("date", ""))
    return True, _("live RGB confirmed on {date}").format(
        date=format_datetime(when) if when is not None else rec.get("date", "?"))


def record(info: dict) -> Tuple[str, dict]:
    return device_key(info), {"firmware": info.get("firmware"),
                              "date": time.strftime("%Y-%m-%d %H:%M")}


def test_pattern(layout) -> dict:
    """Left half red, right half blue, function row green - easy to describe."""
    keys = {}
    for k in layout.keys:
        if k.row == 0:
            keys[str(k.index)] = "#00ff00"
        elif k.u >= 0.5:
            keys[str(k.index)] = "#0000ff"
    return {"effect": "static", "params": {"color": "#ff0000"}, "keys": keys, "reaction": {}}


TEST_DESCRIPTION = N_("Function-key row (Esc, F1-F12, PrtSc...) GREEN, the left half of the "
                      "other keys RED and the right half BLUE.")
