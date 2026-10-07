# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Plain coordinate builders shared by the ArcPy and Rasterio writers."""

from __future__ import annotations

import math


def circle_ring(x: float, y: float, radius: float, segments: int = 72) -> list[tuple[float, float]]:
    """Closed clockwise ring approximating a circle."""
    points = [
        (x + radius * math.sin(2.0 * math.pi * index / segments), y + radius * math.cos(2.0 * math.pi * index / segments))
        for index in range(segments)
    ]
    points.append(points[0])
    return points


def sector_ring(
    x: float, y: float, azimuth_deg: float, half_width_deg: float, radius: float, segments: int = 16
) -> list[tuple[float, float]]:
    """Closed wedge from the LZ centre out to ``radius`` around ``azimuth_deg``."""
    points = [(x, y)]
    for index in range(segments + 1):
        angle = math.radians(azimuth_deg - half_width_deg + 2.0 * half_width_deg * index / segments)
        points.append((x + radius * math.sin(angle), y + radius * math.cos(angle)))
    points.append((x, y))
    return points


def approach_line(x: float, y: float, azimuth_deg: float, length: float) -> list[tuple[float, float]]:
    """Line drawn from the start of the approach toward the LZ (arrow points in)."""
    angle = math.radians(azimuth_deg)
    return [(x + length * math.sin(angle), y + length * math.cos(angle)), (x, y)]
