# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Choosing a metric (UTM) coordinate system for a latitude/longitude DEM (v0.9.0). Pure Python: no GDAL or ArcPy needed."""

from __future__ import annotations

import math

# EPSG codes: WGS 84 / UTM north = 32601..32660, south = 32701..32760; NAD83 / UTM north = 26901..26923 (zones 1-23 only).
NAD83_GEOGRAPHIC_EPSG = {4269, 4617, 6318}  # NAD83, NAD83(CSRS), NAD83(2011)
NAD83_MAX_ZONE = 23


class ReprojectError(ValueError):
    """The DEM cannot be placed in a UTM zone."""


def utm_zone(longitude: float) -> int:
    lon = ((float(longitude) + 180.0) % 360.0) - 180.0
    return min(60, max(1, int(math.floor((lon + 180.0) / 6.0)) + 1))


def utm_epsg(longitude: float, latitude: float, source_geographic_epsg: int | None = None, datum_name: str = "") -> tuple[int, str]:
    """EPSG code and label of the UTM zone for a position. The datum of the DEM is kept where a UTM definition exists
    (NAD83 in zones 1-23 north), otherwise WGS 84 is used. Latitudes beyond 84 N / 80 S have no UTM zone."""
    if not (-80.0 <= latitude <= 84.0):
        raise ReprojectError(f"Latitude {latitude:.2f} is outside the UTM range (80 S to 84 N): project the DEM yourself to a polar system.")
    zone = utm_zone(longitude)
    north = latitude >= 0.0
    nad83 = source_geographic_epsg in NAD83_GEOGRAPHIC_EPSG or "nad83" in datum_name.lower().replace(" ", "").replace("_", "") \
        or "nad1983" in datum_name.lower().replace(" ", "").replace("_", "") \
        or "northamerican1983" in datum_name.lower().replace(" ", "").replace("_", "")
    if nad83 and north and zone <= NAD83_MAX_ZONE:
        return 26900 + zone, f"NAD83 / UTM zone {zone}N"
    return (32600 if north else 32700) + zone, f"WGS 84 / UTM zone {zone}{'N' if north else 'S'}"


def metres_per_degree(latitude: float) -> tuple[float, float]:
    """(metres per degree of longitude, metres per degree of latitude) at a latitude (WGS 84 series)."""
    phi = math.radians(latitude)
    lat_m = 111132.954 - 559.822 * math.cos(2.0 * phi) + 1.175 * math.cos(4.0 * phi)
    lon_m = 111412.84 * math.cos(phi) - 93.5 * math.cos(3.0 * phi) + 0.118 * math.cos(5.0 * phi)
    return lon_m, lat_m


def square_cell_m(cell_lon_deg: float, cell_lat_deg: float, latitude: float) -> float:
    """Edge of the square metric cell with the same area as a degree cell at ``latitude`` (rounded to 1 mm)."""
    lon_m, lat_m = metres_per_degree(latitude)
    return round(math.sqrt(abs(cell_lon_deg) * lon_m * abs(cell_lat_deg) * lat_m), 3)
