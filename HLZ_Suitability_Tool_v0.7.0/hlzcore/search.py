# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Search around a point (v0.6.7).

A search is a circle (or several) of a given radius around one or more centre points.
Landing-zone centres are only placed inside it. It can be used on its own, or together with
an area-of-interest polygon, in which case the overlap is searched.

This module is pure NumPy (no GIS library):

* ``parse_search_centre`` - turns a typed MGRS grid reference or a latitude/longitude into
  decimal degrees (WGS84);
* ``SearchArea``          - the circles: the grid window they cover, a boolean mask for any
  tile, and the distance and bearing of any point from the nearest centre.
"""

from __future__ import annotations

import math
import re
from typing import Sequence

import numpy as np

from .geo import _A, _BANDS, _COLUMN_SETS, _E2, _EP2, _K0, _ROW_LETTERS, latlon_to_utm
from .models import GridGeometry


# ---------------------------------------------------------------------------
# Coordinates typed by a person
# ---------------------------------------------------------------------------


def utm_to_latlon(zone: int, northern: bool, easting: float, northing: float) -> tuple[float, float]:
    """Inverse UTM on WGS84 (Snyder series); returns (latitude, longitude) in degrees."""
    x = easting - 500000.0
    y = northing if northern else northing - 10_000_000.0
    mu = (y / _K0) / (_A * (1.0 - _E2 / 4.0 - 3.0 * _E2**2 / 64.0 - 5.0 * _E2**3 / 256.0))
    e1 = (1.0 - math.sqrt(1.0 - _E2)) / (1.0 + math.sqrt(1.0 - _E2))
    footprint = (
        mu
        + (3.0 * e1 / 2.0 - 27.0 * e1**3 / 32.0) * math.sin(2.0 * mu)
        + (21.0 * e1**2 / 16.0 - 55.0 * e1**4 / 32.0) * math.sin(4.0 * mu)
        + (151.0 * e1**3 / 96.0) * math.sin(6.0 * mu)
        + (1097.0 * e1**4 / 512.0) * math.sin(8.0 * mu)
    )
    sin_f, cos_f, tan_f = math.sin(footprint), math.cos(footprint), math.tan(footprint)
    c1 = _EP2 * cos_f**2
    t1 = tan_f**2
    n1 = _A / math.sqrt(1.0 - _E2 * sin_f**2)
    r1 = _A * (1.0 - _E2) / (1.0 - _E2 * sin_f**2) ** 1.5
    d = x / (n1 * _K0)
    latitude = footprint - (n1 * tan_f / r1) * (
        d**2 / 2.0
        - (5.0 + 3.0 * t1 + 10.0 * c1 - 4.0 * c1**2 - 9.0 * _EP2) * d**4 / 24.0
        + (61.0 + 90.0 * t1 + 298.0 * c1 + 45.0 * t1**2 - 252.0 * _EP2 - 3.0 * c1**2) * d**6 / 720.0
    )
    central = math.radians((zone - 1) * 6 - 180 + 3)
    longitude = central + (
        d
        - (1.0 + 2.0 * t1 + c1) * d**3 / 6.0
        + (5.0 - 2.0 * c1 + 28.0 * t1 - 3.0 * c1**2 + 8.0 * _EP2 + 24.0 * t1**2) * d**5 / 120.0
    ) / cos_f
    return math.degrees(latitude), math.degrees(longitude)


_MGRS = re.compile(r"^(\d{1,2})([C-HJ-NP-X])([A-HJ-NP-Z])([A-HJ-NP-V])(\d*)$")


def mgrs_to_latlon(text: str) -> tuple[float, float]:
    """MGRS grid reference to (latitude, longitude). The centre of the referenced square is used.

    Accepts spaces or none: ``18T VR 45906 30941``, ``18TVR4590630941``, ``18T VR 4590 3094``
    (10 m), ``18T VR 45 30`` (1 km) or ``18T VR`` (the 100 km square).
    """
    compact = re.sub(r"[\s,]+", "", text.upper())
    match = _MGRS.match(compact)
    if match is None:
        raise ValueError(f"'{text}' is not a valid MGRS grid reference (example: 18T VR 45906 30941)")
    zone = int(match.group(1))
    band, column, row, digits = match.group(2), match.group(3), match.group(4), match.group(5)
    if not 1 <= zone <= 60:
        raise ValueError(f"MGRS zone {zone} is not between 1 and 60")
    if len(digits) % 2 or len(digits) > 10:
        raise ValueError("The MGRS digits must come in an equal number for easting and northing (2 to 10 in total)")
    half = len(digits) // 2
    unit = 10 ** (5 - half)
    east = (int(digits[:half]) if half else 0) * unit + unit / 2.0
    north = (int(digits[half:]) if half else 0) * unit + unit / 2.0
    column_letters = _COLUMN_SETS[(zone - 1) % 3]
    if column not in column_letters:
        raise ValueError(f"Column letter {column} does not exist in MGRS zone {zone}")
    easting = (column_letters.index(column) + 1) * 100_000.0 + east
    row_index = _ROW_LETTERS.index(row)
    if zone % 2 == 0:
        row_index = (row_index - 5) % 20
    base = row_index * 100_000.0 + north
    latitude_min = -80.0 + 8.0 * _BANDS.index(band)
    central_meridian = float((zone - 1) * 6 - 180 + 3)
    reference = latlon_to_utm(latitude_min, central_meridian, zone)[3]
    cycles = max(0, math.ceil((reference - 50_000.0 - base) / 2_000_000.0))
    northing = base + cycles * 2_000_000.0
    latitude, longitude = utm_to_latlon(zone, band >= "N", easting, northing)
    if not latitude_min - 0.5 <= latitude <= latitude_min + 8.5 + (4.0 if band == "X" else 0.0):
        raise ValueError(f"'{text}' does not lie inside latitude band {band}")
    return latitude, longitude


_NUMBER = r"-?\d+(?:\.\d+)?"
_DECIMAL = re.compile(
    rf"^\s*([NSEW])?\s*({_NUMBER})\s*[°º]?\s*([NSEW])?\s*[,;\s]\s*([NSEW])?\s*({_NUMBER})\s*[°º]?\s*([NSEW])?\s*$",
    re.IGNORECASE,
)
_DMS = re.compile(
    r"^\s*(\d+)\s*[°º]\s*(\d+)\s*['′’]\s*(\d+(?:\.\d+)?)\s*[\"″”]?\s*([NS])\s*[,;\s]\s*"
    r"(\d+)\s*[°º]\s*(\d+)\s*['′’]\s*(\d+(?:\.\d+)?)\s*[\"″”]?\s*([EW])\s*$",
    re.IGNORECASE,
)


def _signed(value: float, letter: str | None) -> float:
    if letter and letter.upper() in "SW":
        return -abs(value)
    return value


def parse_search_centre(text: str) -> tuple[float, float, str]:
    """Parse a typed centre into (latitude, longitude, kind) with kind ``MGRS`` or ``LATLON``.

    Accepted forms (WGS84):
      * MGRS: ``18T VR 45906 30941``
      * decimal degrees: ``45.4236, -75.7009``, ``45.4236 N 75.7009 W``, ``N45.4236 W75.7009``
      * degrees, minutes, seconds: ``45°25'25"N 75°42'03"W``
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("The search centre is empty")
    if re.match(r"^\d{1,2}\s*[C-HJ-NP-X]\s*[A-HJ-NP-Z]\s*[A-HJ-NP-V]\s*[\d\s]*$", raw.upper()):
        latitude, longitude = mgrs_to_latlon(raw)
        return latitude, longitude, "MGRS"
    match = _DMS.match(raw)
    if match:
        d1, m1, s1, h1, d2, m2, s2, h2 = match.groups()
        latitude = _signed(int(d1) + int(m1) / 60.0 + float(s1) / 3600.0, h1)
        longitude = _signed(int(d2) + int(m2) / 60.0 + float(s2) / 3600.0, h2)
    else:
        match = _DECIMAL.match(raw)
        if match is None:
            raise ValueError(
                f"'{text}' is not a recognised position. Use an MGRS reference (18T VR 45906 30941) or "
                "latitude, longitude (45.4236, -75.7009)"
            )
        a_pre, a_value, a_post, b_pre, b_value, b_post = match.groups()
        first_letter = (a_pre or a_post or "").upper()
        second_letter = (b_pre or b_post or "").upper()
        first, second = _signed(float(a_value), first_letter), _signed(float(b_value), second_letter)
        if first_letter in ("E", "W") or second_letter in ("N", "S"):
            first, second = second, first  # longitude was typed first
        latitude, longitude = first, second
    if not -90.0 <= latitude <= 90.0:
        raise ValueError(f"Latitude {latitude} is outside -90 to 90")
    if not -180.0 <= longitude <= 180.0:
        raise ValueError(f"Longitude {longitude} is outside -180 to 180")
    return latitude, longitude, "LATLON"


# ---------------------------------------------------------------------------
# The search circles
# ---------------------------------------------------------------------------


class SearchArea:
    """One or more circles of equal radius, in the coordinates of the elevation grid."""

    def __init__(self, centres: Sequence[tuple[float, float]], radius_m: float):
        self.centres = [(float(x), float(y)) for x, y in centres]
        self.radius_m = float(radius_m)
        if not self.centres:
            raise ValueError("A search area needs at least one centre")
        if not self.radius_m > 0.0:
            raise ValueError("The search radius must be greater than zero")

    @property
    def digest(self) -> str:
        points = ";".join(f"{x:.1f},{y:.1f}" for x, y in sorted(self.centres)[:50])
        return f"circles:r={self.radius_m:.1f}:n={len(self.centres)}:{points}"

    @property
    def description(self) -> str:
        count = len(self.centres)
        return f"radius {self.radius_m:,.0f} m around {count} point{'s' if count != 1 else ''}"

    def window(self, geometry: GridGeometry) -> tuple[int, int, int, int] | None:
        """(row0, row1, col0, col1) of the grid cells the circles' bounding box covers, or None."""
        xs = [x for x, _ in self.centres]
        ys = [y for _, y in self.centres]
        x_min, x_max = min(xs) - self.radius_m, max(xs) + self.radius_m
        y_min, y_max = min(ys) - self.radius_m, max(ys) + self.radius_m
        col0 = max(0, int(math.floor((x_min - geometry.x_origin) / geometry.pixel_width)))
        col1 = min(geometry.width, int(math.ceil((x_max - geometry.x_origin) / geometry.pixel_width)))
        row0 = max(0, int(math.floor((y_max - geometry.y_origin) / geometry.pixel_height)))
        row1 = min(geometry.height, int(math.ceil((y_min - geometry.y_origin) / geometry.pixel_height)))
        if col1 <= col0 or row1 <= row0:
            return None
        return row0, row1, col0, col1

    def mask(self, tile: GridGeometry) -> np.ndarray:
        """Boolean mask of the tile's cells whose centres lie within the radius of any centre."""
        columns = tile.x_origin + (np.arange(tile.width) + 0.5) * tile.pixel_width
        rows = tile.y_origin + (np.arange(tile.height) + 0.5) * tile.pixel_height
        result = np.zeros((tile.height, tile.width), dtype=bool)
        limit = self.radius_m * self.radius_m
        for cx, cy in self.centres:
            col_hit = np.flatnonzero(np.abs(columns - cx) <= self.radius_m)
            row_hit = np.flatnonzero(np.abs(rows - cy) <= self.radius_m)
            if col_hit.size == 0 or row_hit.size == 0:
                continue
            dx2 = (columns[col_hit] - cx) ** 2
            dy2 = (rows[row_hit] - cy) ** 2
            inside = (dy2[:, None] + dx2[None, :]) <= limit
            block = result[row_hit[0] : row_hit[-1] + 1, col_hit[0] : col_hit[-1] + 1]
            block |= inside
        return result

    def nearest(self, x: float, y: float) -> tuple[float, float]:
        """Distance (m) and true bearing (degrees, 0 = north) from the nearest centre to (x, y)."""
        best = min(self.centres, key=lambda c: (x - c[0]) ** 2 + (y - c[1]) ** 2)
        dx, dy = x - best[0], y - best[1]
        return math.hypot(dx, dy), (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def intersect_windows(
    first: tuple[int, int, int, int], second: tuple[int, int, int, int]
) -> tuple[int, int, int, int] | None:
    """Intersection of two (row0, row1, col0, col1) windows, or None."""
    row0, row1 = max(first[0], second[0]), min(first[1], second[1])
    col0, col1 = max(first[2], second[2]), min(first[3], second[3])
    return (row0, row1, col0, col1) if row1 > row0 and col1 > col0 else None


def combine_masks(shape: tuple[int, int], polygon_mask: np.ndarray | None, circle_mask: np.ndarray | None) -> np.ndarray | None:
    """AND of the area-of-interest mask and the search-circle mask. None means every cell is inside."""
    if polygon_mask is None and circle_mask is None:
        return None
    if polygon_mask is None:
        return circle_mask
    if circle_mask is None:
        return polygon_mask
    return polygon_mask & circle_mask
