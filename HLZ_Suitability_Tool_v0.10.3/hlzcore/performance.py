# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Approximate atmospheric screening calculations.

These are standard rule-of-thumb approximations for planning screening only.
They never replace the aircraft operator's performance charts.
"""

from __future__ import annotations

FEET_PER_METRE = 3.280839895013123
STANDARD_ALTIMETER_INHG = 29.92
HPA_PER_INHG = 33.8638866667


def hpa_to_inhg(pressure_hpa: float) -> float:
    return float(pressure_hpa) / HPA_PER_INHG


def pressure_altitude_ft(elevation_ft: float, altimeter_inhg: float) -> float:
    """Return approximate pressure altitude in feet (1 inHg ~ 1000 ft)."""
    return float(elevation_ft) + (STANDARD_ALTIMETER_INHG - float(altimeter_inhg)) * 1000.0


def isa_temperature_c(pressure_altitude: float) -> float:
    """ISA temperature at a pressure altitude (1.98 C per 1000 ft lapse)."""
    return 15.0 - 1.98 * (float(pressure_altitude) / 1000.0)


def density_altitude_ft(
    elevation_m: float, oat_c: float, altimeter_inhg: float = STANDARD_ALTIMETER_INHG
) -> float:
    """Return approximate density altitude (120 ft per C above ISA)."""
    if not 25.0 <= float(altimeter_inhg) <= 32.5:
        raise ValueError(
            f"Altimeter setting {altimeter_inhg} inHg is outside 25.0-32.5; "
            "check the units (use inHg, or convert hPa with hpa_to_inhg)"
        )
    if not -70.0 <= float(oat_c) <= 60.0:
        raise ValueError(f"Outside air temperature {oat_c} C is outside -70 to +60 C")
    elevation_ft = float(elevation_m) * FEET_PER_METRE
    pressure_altitude = pressure_altitude_ft(elevation_ft, altimeter_inhg)
    return pressure_altitude + 120.0 * (float(oat_c) - isa_temperature_c(pressure_altitude))
