# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Exclusion-area helpers (v0.7.0): polygon, line and point rasterisation and distances.

Water bodies, wetlands and other unsuitable surfaces arrive as polygons. This
module turns them into a boolean raster aligned with the elevation grid and
measures how far every cell is from the nearest excluded cell.

Only NumPy and SciPy are used, so it runs in the default ArcGIS Pro Python.

* ``rasterize_polygons`` - even-odd scanline fill. A cell is inside when its
  CENTRE is inside; interior rings (islands in a lake) are holes. The cost is
  proportional to the number of polygon edges and rows, not to the number of
  cells, so a lake with thousands of vertices fills a 16 M-cell tile quickly.
* ``rasterize_lines`` - rivers, streams and shorelines arrive as lines, which have no width. Every cell a
  line passes through is marked, then widened to ``width_m`` (total width, so half on each side).
  A closed line is NOT filled: a lake outline excludes only the strip along the shore.
* ``distance_to_mask_m`` - distance in metres from every cell to the nearest
  True cell (Euclidean distance transform).
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np
from scipy import ndimage

from .models import GridGeometry

Ring = Sequence[tuple]
Polygon = Sequence[Ring]  # outer ring plus holes; all rings follow the even-odd rule


def _fill_rings(rings: Polygon, geometry: GridGeometry, mask: np.ndarray) -> None:
    """OR the even-odd fill of ``rings`` into ``mask`` (same shape as the grid)."""
    pixel_width = geometry.pixel_width
    pixel_height = geometry.pixel_height
    if pixel_height >= 0.0 or pixel_width <= 0.0:
        raise ValueError("Only north-up grids (positive width, negative height) are supported")
    x0 = geometry.x_origin
    y0 = geometry.y_origin
    height, width = mask.shape

    xs1: list[np.ndarray] = []
    ys1: list[np.ndarray] = []
    xs2: list[np.ndarray] = []
    ys2: list[np.ndarray] = []
    for ring in rings:
        points = np.asarray(ring, dtype=np.float64)
        if points.ndim != 2 or len(points) < 3:
            continue
        if not np.array_equal(points[0], points[-1]):
            points = np.vstack([points, points[:1]])
        xs1.append(points[:-1, 0])
        ys1.append(points[:-1, 1])
        xs2.append(points[1:, 0])
        ys2.append(points[1:, 1])
    if not xs1:
        return
    x1 = np.concatenate(xs1)
    y1 = np.concatenate(ys1)
    x2 = np.concatenate(xs2)
    y2 = np.concatenate(ys2)
    keep = y1 != y2  # horizontal edges never cross a scanline
    x1, y1, x2, y2 = x1[keep], y1[keep], x2[keep], y2[keep]
    if x1.size == 0:
        return

    # Rows whose centre line crosses each edge: ylo <= yc(r) < yhi, yc(r) = y0 + (r + 0.5) * ph (ph < 0).
    y_low = np.minimum(y1, y2)
    y_high = np.maximum(y1, y2)
    first_row = np.floor((y_high - y0) / pixel_height - 0.5).astype(np.int64) + 1
    last_row = np.floor((y_low - y0) / pixel_height - 0.5).astype(np.int64)
    first_row = np.maximum(first_row, 0)
    last_row = np.minimum(last_row, height - 1)
    counts = last_row - first_row + 1
    usable = counts > 0
    if not usable.any():
        return
    x1, y1, x2, y2 = x1[usable], y1[usable], x2[usable], y2[usable]
    first_row, counts = first_row[usable], counts[usable]

    edge_index = np.repeat(np.arange(len(counts)), counts)
    offsets = np.arange(int(counts.sum())) - np.repeat(np.cumsum(counts) - counts, counts)
    rows = first_row[edge_index] + offsets
    y_centre = y0 + (rows + 0.5) * pixel_height
    x_cross = x1[edge_index] + (y_centre - y1[edge_index]) * (x2[edge_index] - x1[edge_index]) / (
        y2[edge_index] - y1[edge_index]
    )

    order = np.lexsort((x_cross, rows))
    rows = rows[order]
    x_cross = x_cross[order]

    # Pair the sorted crossings of each row: 1st-2nd, 3rd-4th, ... (an odd leftover is dropped).
    starts = np.flatnonzero(np.concatenate(([True], rows[1:] != rows[:-1])))
    row_lengths = np.diff(np.concatenate((starts, [len(rows)])))
    position = np.arange(len(rows)) - np.repeat(starts, row_lengths)
    paired_length = np.repeat(row_lengths - (row_lengths % 2), row_lengths)
    valid = position < paired_length
    opens = valid & (position % 2 == 0)
    closes = valid & (position % 2 == 1)
    open_rows = rows[opens]
    x_open = x_cross[opens]
    x_close = x_cross[closes]
    if open_rows.size == 0:
        return

    # A cell centre xc(c) = x0 + (c + 0.5) * pw lies in [x_open, x_close).
    col_start = np.ceil((x_open - x0) / pixel_width - 0.5).astype(np.int64)
    col_end = np.ceil((x_close - x0) / pixel_width - 0.5).astype(np.int64)
    col_start = np.clip(col_start, 0, width)
    col_end = np.clip(col_end, 0, width)
    span = col_end > col_start
    if not span.any():
        return
    open_rows, col_start, col_end = open_rows[span], col_start[span], col_end[span]

    row_min = int(open_rows.min())
    row_max = int(open_rows.max())
    stride = width + 1
    flat_start = (open_rows - row_min) * stride + col_start
    flat_end = (open_rows - row_min) * stride + col_end
    size = (row_max - row_min + 1) * stride
    difference = np.bincount(flat_start, minlength=size) - np.bincount(flat_end, minlength=size)
    filled = np.cumsum(difference.reshape(row_max - row_min + 1, stride), axis=1)[:, :width] > 0
    mask[row_min : row_max + 1, :] |= filled


def rasterize_polygons(polygons: Sequence[Polygon], geometry: GridGeometry) -> np.ndarray:
    """Boolean mask of the cells whose centres fall inside any polygon.

    ``polygons`` is a list; each polygon is a list of rings (lists of ``(x, y)``)
    in the coordinate system of ``geometry``. Rings after the first are holes by
    the even-odd rule, so a lake with islands is a single polygon.
    """
    mask = np.zeros((geometry.height, geometry.width), dtype=bool)
    pixel_width = geometry.pixel_width
    pixel_height = geometry.pixel_height
    for rings in polygons:
        all_x: list[float] = []
        all_y: list[float] = []
        for ring in rings:
            for point in ring:
                all_x.append(point[0])
                all_y.append(point[1])
        if len(all_x) < 3:
            continue
        col0 = max(0, int(math.floor((min(all_x) - geometry.x_origin) / pixel_width)))
        col1 = min(geometry.width, int(math.ceil((max(all_x) - geometry.x_origin) / pixel_width)) + 1)
        row0 = max(0, int(math.floor((max(all_y) - geometry.y_origin) / pixel_height)))
        row1 = min(geometry.height, int(math.ceil((min(all_y) - geometry.y_origin) / pixel_height)) + 1)
        if col1 <= col0 or row1 <= row0:
            continue
        window = GridGeometry(
            x_origin=geometry.x_origin + col0 * pixel_width,
            y_origin=geometry.y_origin + row0 * pixel_height,
            pixel_width=pixel_width,
            pixel_height=pixel_height,
            width=col1 - col0,
            height=row1 - row0,
        )
        sub = np.zeros((window.height, window.width), dtype=bool)
        _fill_rings(rings, window, sub)
        mask[row0:row1, col0:col1] |= sub
    return mask


Line = Sequence[tuple]  # one part of a polyline: (x, y) points


def rasterize_lines(lines: Sequence[Line], geometry: GridGeometry, width_m: float) -> np.ndarray:
    """Boolean mask of the cells within ``width_m / 2`` of any line (and every cell a line passes through).

    ``lines`` is a list of parts; each part is a list of ``(x, y)`` in the coordinate system of ``geometry``.
    Lines just outside the grid still count when they are within the half-width of it.
    """
    height, width = geometry.height, geometry.width
    mask = np.zeros((height, width), dtype=bool)
    pixel = abs(geometry.pixel_width)
    half = max(float(width_m), 0.0) / 2.0
    pad = int(math.ceil(half / pixel)) + 1
    big = np.zeros((height + 2 * pad, width + 2 * pad), dtype=bool)
    x0 = geometry.x_origin - pad * geometry.pixel_width
    y0 = geometry.y_origin - pad * geometry.pixel_height  # pixel_height < 0
    step = pixel / 2.0
    for part in lines:
        points = np.asarray(part, dtype=np.float64)
        if points.ndim != 2 or len(points) < 1:
            continue
        if len(points) == 1:
            points = np.vstack([points, points])
        for a, b in zip(points[:-1], points[1:]):
            length = float(math.hypot(b[0] - a[0], b[1] - a[1]))
            count = max(int(math.ceil(length / step)), 1) + 1
            t = np.linspace(0.0, 1.0, count)
            xs = a[0] + t * (b[0] - a[0])
            ys = a[1] + t * (b[1] - a[1])
            cols = np.floor((xs - x0) / geometry.pixel_width).astype(np.int64)
            rows = np.floor((ys - y0) / geometry.pixel_height).astype(np.int64)
            keep = (cols >= 0) & (cols < big.shape[1]) & (rows >= 0) & (rows < big.shape[0])
            big[rows[keep], cols[keep]] = True
    if not big.any():
        return mask
    if half > 0.0:
        distance = ndimage.distance_transform_edt(~big) * pixel
        big = distance <= half
    return big[pad : pad + height, pad : pad + width].copy()


Point = tuple  # one (x, y) position


def rasterize_points(points: Sequence[Point], geometry: GridGeometry, radius_m: float) -> np.ndarray:
    """Boolean mask of the cells whose centres lie within ``radius_m`` of any point (v0.7.0).

    Used for point exclusions (a known hazard, a pole, a crane, a wellhead). A point just outside the grid
    still counts when it is within ``radius_m`` of it. A radius of 0 marks only the cell that holds the point.
    """
    height, width = geometry.height, geometry.width
    mask = np.zeros((height, width), dtype=bool)
    pixel_w = geometry.pixel_width
    pixel_h = geometry.pixel_height
    radius = max(float(radius_m), 0.0)
    limit_sq = radius * radius
    reach_cols = int(math.ceil(radius / abs(pixel_w))) + 1
    reach_rows = int(math.ceil(radius / abs(pixel_h))) + 1
    for point in points:
        x, y = float(point[0]), float(point[1])
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        col = int(math.floor((x - geometry.x_origin) / pixel_w))
        row = int(math.floor((y - geometry.y_origin) / pixel_h))
        r0 = max(0, row - reach_rows)
        r1 = min(height, row + reach_rows + 1)
        c0 = max(0, col - reach_cols)
        c1 = min(width, col + reach_cols + 1)
        if r1 <= r0 or c1 <= c0:
            if radius == 0.0 and 0 <= row < height and 0 <= col < width:
                mask[row, col] = True
            continue
        rows = np.arange(r0, r1)
        cols = np.arange(c0, c1)
        centre_y = geometry.y_origin + (rows + 0.5) * pixel_h
        centre_x = geometry.x_origin + (cols + 0.5) * pixel_w
        distance_sq = (centre_y[:, None] - y) ** 2 + (centre_x[None, :] - x) ** 2
        inside = distance_sq <= limit_sq
        mask[r0:r1, c0:c1] |= inside
        if 0 <= row < height and 0 <= col < width:
            mask[row, col] = True  # the cell that holds the point is always excluded
    return mask


def distance_to_mask_m(mask: np.ndarray, cell_size_m: float) -> np.ndarray:
    """Distance in metres from every cell to the nearest True cell (float32).

    The mask must contain at least one True cell.
    """
    if not mask.any():
        raise ValueError("The mask has no True cell")
    distance = ndimage.distance_transform_edt(~mask)
    return (distance * float(cell_size_m)).astype(np.float32)
