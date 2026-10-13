# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Length units for the report (v0.8.0). The analysis is always in metres; only the display changes.

``METRIC``  : 12.3 m
``BOTH``    : 12.3 m (40.4 ft)      (default)
``IMPERIAL``: 40.4 ft (12.3 m)
"""

from __future__ import annotations

M_TO_FT = 1.0 / 0.3048  # the international foot is exactly 0.3048 m

UNIT_MODES = ("METRIC", "BOTH", "IMPERIAL")
DEFAULT_UNITS = "BOTH"


def validate_units(value: str | None) -> str:
    mode = (value or DEFAULT_UNITS).strip().upper()
    aliases = {"M": "METRIC", "METRES": "METRIC", "METERS": "METRIC", "FEET": "IMPERIAL", "FT": "IMPERIAL",
               "METRES_AND_FEET": "BOTH", "METERS_AND_FEET": "BOTH", "BOTH UNITS": "BOTH"}
    mode = aliases.get(mode, mode)
    if mode not in UNIT_MODES:
        raise ValueError(f"Unknown report units '{value}'. Use one of: {', '.join(UNIT_MODES)}")
    return mode


def m_to_ft(value_m: float | None) -> float | None:
    return None if value_m is None else float(value_m) * M_TO_FT


def fmt_length(value_m: float | None, digits: int = 1, mode: str = DEFAULT_UNITS, ft_digits: int | None = None) -> str:
    """Format a length given in metres. ``None`` or NaN gives an empty string."""
    if value_m is None:
        return ""
    value = float(value_m)
    if value != value or value in (float("inf"), float("-inf")):
        return ""
    ft_digits = digits if ft_digits is None else ft_digits
    metres = f"{value:,.{digits}f} m"
    feet = f"{value * M_TO_FT:,.{ft_digits}f} ft"
    mode = validate_units(mode)
    if mode == "METRIC":
        return metres
    if mode == "IMPERIAL":
        return f"{feet} ({metres})"
    return f"{metres} ({feet})"
