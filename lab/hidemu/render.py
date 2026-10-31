# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""SVG picture of what the emulated keyboard shows (its state, not the program's).

HP/LampArray: lamp colours as latched by the device (LampUpdateComplete), drawn at
the key the lamp sits under (the lab's own key table).
Kingston/vendor: the 380-byte direct-mode buffer decoded per protocol.py; the index ->
key placement uses the project's layout (only for drawing - the vendor index map
cannot be verified without the physical keyboard).
"""

from __future__ import annotations

import html
from typing import Dict, Optional, Tuple

from . import keytable

U = 34          # pixels per key unit
PAD = 2


def _fill(rgb: Optional[Tuple[int, int, int]]) -> str:
    if rgb is None:
        return "#3a3a3a"
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb)


def _vendor_colors(dev, colors) -> Dict[str, Tuple[int, int, int]]:
    """Vendor buffer index -> key name, through the project's layout (drawing only)."""
    try:
        import sys
        from pathlib import Path
        root = str(Path(__file__).resolve().parents[2])
        if root not in sys.path:
            sys.path.insert(0, root)
        from openalloy.domain.layout import HID_USAGE, get_layout
    except ImportError:
        return {}
    layout = get_layout(dev.variant)
    by_usage = keytable.by_usage(dev.variant)
    out = {}
    for k in layout.keys:
        usage = HID_USAGE.get(k.index)
        key = by_usage.get(usage) if usage else (
            next((x for x in keytable.keys(dev.variant) if x.name == "fn"), None)
            if k.name == "fn" else None)
        if key is not None and k.index < len(colors):
            out[key.name] = colors[k.index]
    return out


def svg(dev, title: str = "") -> str:
    shown = dev.shown()
    mode = shown.get("mode")
    colors = shown.get("colors")
    if colors is not None and isinstance(colors, list):
        colors = _vendor_colors(dev, colors)
    keys = keytable.keys(dev.variant)
    w = int(keytable.WIDTH_U * U) + 20
    h = int(keytable.HEIGHT_U * U) + 60
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
             f'viewBox="0 0 {w} {h}" font-family="sans-serif">',
             f'<rect width="{w}" height="{h}" rx="10" fill="#151515"/>']
    label = {"host": "host (LampArray)", "direct": "direct mode (vendor)",
             "firmware": "firmware lighting", "onboard": "onboard profile"}.get(mode, mode)
    parts.append(f'<text x="10" y="22" fill="#ddd" font-size="14">'
                 f'{html.escape(title or dev.MODEL)} - {html.escape(str(label))}</text>')
    for k in keys:
        x = 10 + k.x * U + PAD
        y = 40 + k.y * U + PAD
        if colors is None:
            rgb = None
        else:
            rgb = colors.get(k.name)
        fill = _fill(rgb) if colors is not None else "url(#fw)"
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{k.w * U - 2 * PAD:.1f}" '
                     f'height="{U - 2 * PAD}" rx="4" fill="{fill}" stroke="#000"/>')
        lum = sum(rgb) / 3 if rgb else 0
        parts.append(f'<text x="{x + 3:.1f}" y="{y + 12:.1f}" font-size="8" '
                     f'fill="{"#000" if lum > 140 else "#bbb"}">{html.escape(k.name)}</text>')
    parts.insert(2, '<defs><linearGradient id="fw" x1="0" x2="1"><stop offset="0" '
                    'stop-color="#a33"/><stop offset=".5" stop-color="#3a3"/><stop offset="1" '
                    'stop-color="#33a"/></linearGradient></defs>')
    parts.append("</svg>")
    return "\n".join(parts)
