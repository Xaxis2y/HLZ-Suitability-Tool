# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Pure-Python WGS84 -> UTM -> MGRS conversion for reporting.

Aircrews of the CAF, US forces and NATO partners pass landing-zone locations
as MGRS grid references, so every candidate is reported in MGRS (1 m
precision) and in decimal degrees. No external geodesy package is needed.
Polar regions (UPS, latitude > 84 N or < 80 S) return an empty string.
"""

from __future__ import annotations

import math

_A = 6378137.0
_F = 1.0 / 298.257223563
_K0 = 0.9996
_E2 = _F * (2.0 - _F)
_EP2 = _E2 / (1.0 - _E2)
_BANDS = "CDEFGHJKLMNPQRSTUVWX"
_COLUMN_SETS = ("ABCDEFGH", "JKLMNPQR", "STUVWXYZ")
_ROW_LETTERS = "ABCDEFGHJKLMNPQRSTUV"


def utm_zone(latitude: float, longitude: float) -> int:
    longitude = ((longitude + 180.0) % 360.0) - 180.0
    zone = int((longitude + 180.0) // 6.0) + 1
    if 56.0 <= latitude < 64.0 and 3.0 <= longitude < 12.0:
        zone = 32
    if 72.0 <= latitude <= 84.0:
        if 0.0 <= longitude < 9.0:
            zone = 31
        elif 9.0 <= longitude < 21.0:
            zone = 33
        elif 21.0 <= longitude < 33.0:
            zone = 35
        elif 33.0 <= longitude < 42.0:
            zone = 37
    return min(max(zone, 1), 60)


def latitude_band(latitude: float) -> str:
    if latitude < -80.0 or latitude > 84.0:
        return ""
    index = int((latitude + 80.0) // 8.0)
    return _BANDS[min(index, len(_BANDS) - 1)]


def latlon_to_utm(latitude: float, longitude: float, zone: int | None = None) -> tuple[int, str, float, float]:
    """Return (zone, band, easting, northing) on WGS84."""
    zone = utm_zone(latitude, longitude) if zone is None else zone
    phi = math.radians(latitude)
    lam = math.radians(longitude)
    lam0 = math.radians((zone - 1) * 6 - 180 + 3)
    sin_phi = math.sin(phi)
    cos_phi = math.cos(phi)
    tan_phi = math.tan(phi)
    n = _A / math.sqrt(1.0 - _E2 * sin_phi * sin_phi)
    t = tan_phi * tan_phi
    c = _EP2 * cos_phi * cos_phi
    a = (lam - lam0) * cos_phi
    e4 = _E2 * _E2
    e6 = e4 * _E2
    m = _A * (
        (1.0 - _E2 / 4.0 - 3.0 * e4 / 64.0 - 5.0 * e6 / 256.0) * phi
        - (3.0 * _E2 / 8.0 + 3.0 * e4 / 32.0 + 45.0 * e6 / 1024.0) * math.sin(2.0 * phi)
        + (15.0 * e4 / 256.0 + 45.0 * e6 / 1024.0) * math.sin(4.0 * phi)
        - (35.0 * e6 / 3072.0) * math.sin(6.0 * phi)
    )
    easting = _K0 * n * (
        a
        + (1.0 - t + c) * a**3 / 6.0
        + (5.0 - 18.0 * t + t * t + 72.0 * c - 58.0 * _EP2) * a**5 / 120.0
    ) + 500000.0
    northing = _K0 * (
        m
        + n
        * tan_phi
        * (
            a * a / 2.0
            + (5.0 - t + 9.0 * c + 4.0 * c * c) * a**4 / 24.0
            + (61.0 - 58.0 * t + t * t + 600.0 * c - 330.0 * _EP2) * a**6 / 720.0
        )
    )
    if latitude < 0.0:
        northing += 10_000_000.0
    return zone, latitude_band(latitude), easting, northing


def latlon_to_mgrs(latitude: float | None, longitude: float | None, precision_m: int = 1) -> str:
    """Return an MGRS reference such as ``18T VR 45123 12345`` (1 m precision)."""
    if latitude is None or longitude is None:
        return ""
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return ""
    if latitude < -80.0 or latitude > 84.0:
        return ""
    zone, band, easting, northing = latlon_to_utm(latitude, longitude)
    column_letters = _COLUMN_SETS[(zone - 1) % 3]
    column_index = int(math.floor(easting / 100000.0)) - 1
    if not 0 <= column_index < len(column_letters):
        return ""
    row_index = int(math.floor(northing / 100000.0)) % 20
    if zone % 2 == 0:
        row_index = (row_index + 5) % 20
    digits = {1: 5, 10: 4, 100: 3, 1000: 2, 10000: 1}.get(int(precision_m), 5)
    divisor = 10 ** (5 - digits)
    east = int(math.floor((easting % 100000.0) / divisor))
    north = int(math.floor((northing % 100000.0) / divisor))
    return (
        f"{zone:02d}{band} {column_letters[column_index]}{_ROW_LETTERS[row_index]} "
        f"{east:0{digits}d} {north:0{digits}d}"
    )


def format_latlon(latitude: float | None, longitude: float | None) -> str:
    if latitude is None or longitude is None:
        return ""
    hemisphere_lat = "N" if latitude >= 0 else "S"
    hemisphere_lon = "E" if longitude >= 0 else "W"
    return f"{abs(latitude):.6f} {hemisphere_lat}, {abs(longitude):.6f} {hemisphere_lon}"
