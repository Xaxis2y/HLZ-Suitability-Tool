# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Moving-window terrain and obstacle analysis (v0.6.7).

v0.3 used a Numba brute-force kernel whose cost grew with the square of the
touchdown-point (TDP) radius and which needed a separate Conda environment.
v0.6.7 replaces it with:

* Least-squares plane fitting from FFT-convolved moment sums. Cost no longer
  depends on the window size, and only NumPy/SciPy are needed, so the engine
  runs inside the default ArcGIS Pro Python environment.
* Row tiling with a halo, so memory stays bounded on large AOIs.
* A fast *screening* roughness/obstacle raster (separable maximum filters over
  a union of rectangles inscribed in the disk).
* An *exact* per-candidate re-check (``exact_window_metrics``) that repeats
  the full v0.3 maximum-plane-residual and true-disk obstacle test for every
  candidate before it is accepted. Final candidates are therefore never
  accepted on the approximation alone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

import numpy as np
from scipy import ndimage
from scipy.signal import fftconvolve

GROUND_FAIL = 0
GROUND_PASS = 1
GROUND_UPSLOPE_ONLY = 2
GROUND_NODATA = 255
ROUGHNESS_RMS_FACTOR = 1.4

ProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class TerrainResult:
    """Raster results of the ground analysis (all float32, NaN = no data)."""

    slope_deg: np.ndarray
    aspect_deg: np.ndarray
    roughness_m: np.ndarray
    valid_fraction: np.ndarray
    max_tdp_obstacle_m: np.ndarray
    max_area_obstacle_m: np.ndarray
    tdp_obstacle_coverage: np.ndarray
    area_obstacle_coverage: np.ndarray
    inner_radius_px: float
    outer_radius_px: float
    method: str


def disk_offsets(radius_px: float) -> tuple[np.ndarray, np.ndarray]:
    """Return (dr, dc) integer offsets of cells whose centres lie in a disk."""
    extent = int(math.floor(radius_px))
    span = np.arange(-extent, extent + 1)
    dr, dc = np.meshgrid(span, span, indexing="ij")
    inside = dr * dr + dc * dc <= radius_px * radius_px + 1.0e-9
    return dr[inside], dc[inside]


def disk_kernel(radius_px: float) -> np.ndarray:
    extent = int(math.floor(radius_px))
    span = np.arange(-extent, extent + 1)
    dr, dc = np.meshgrid(span, span, indexing="ij")
    return (dr * dr + dc * dc <= radius_px * radius_px + 1.0e-9).astype(np.float64)


def _correlate(array: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Correlate with zero padding; output aligned with the input."""
    return fftconvolve(array, kernel[::-1, ::-1], mode="same")


def _disk_max_filter(array: np.ndarray, radius_px: float, fill: float) -> np.ndarray:
    """Approximate disk maximum filter using a union of inscribed rectangles.

    Each rectangle is separable, so the cost is O(N) per rectangle regardless
    of radius. The union of 7 rectangles covers ~96 % of the disk; the exact
    per-candidate check covers the remainder.
    """
    radius = max(0.0, float(radius_px))
    result = np.full(array.shape, fill, dtype=array.dtype)
    seen: set[tuple[int, int]] = set()
    for angle in (0.0, 15.0, 30.0, 45.0, 60.0, 75.0, 90.0):
        half_width = int(math.floor(radius * math.cos(math.radians(angle)) + 1.0e-9))
        half_height = int(math.floor(radius * math.sin(math.radians(angle)) + 1.0e-9))
        key = (half_height, half_width)
        if key in seen:
            continue
        seen.add(key)
        filtered = ndimage.maximum_filter(
            array,
            size=(2 * half_height + 1, 2 * half_width + 1),
            mode="constant",
            cval=fill,
        )
        np.maximum(result, filtered, out=result)
    return result


def _det3(a, b, c, d, e, f, g, h, i):  # type: ignore[no-untyped-def]
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def radii_px(cell_size_m: float, tdp_radius_m: float, cleared_ring_m: float) -> tuple[float, float]:
    inner = max(1.0, tdp_radius_m / cell_size_m)
    outer = max(inner, (tdp_radius_m + cleared_ring_m) / cell_size_m)
    return inner, outer


def _analyze_slab(
    dtm: np.ndarray,
    dtm_valid: np.ndarray,
    ndsm: np.ndarray | None,
    ndsm_valid: np.ndarray | None,
    cell_size_m: float,
    inner_px: float,
    outer_px: float,
) -> dict[str, np.ndarray]:
    valid = dtm_valid.astype(np.float64)
    reference = float(np.median(dtm[dtm_valid])) if dtm_valid.any() else 0.0
    z = np.where(dtm_valid, dtm.astype(np.float64) - reference, 0.0)

    kernel = disk_kernel(inner_px)
    extent = (kernel.shape[0] - 1) // 2
    span = np.arange(-extent, extent + 1, dtype=np.float64)
    dr, dc = np.meshgrid(span, span, indexing="ij")
    kx = dc * cell_size_m * kernel
    ky = -dr * cell_size_m * kernel
    expected = float(kernel.sum())

    n = np.rint(_correlate(valid, kernel))
    sx = _correlate(valid, kx)
    sy = _correlate(valid, ky)
    sxx = _correlate(valid, (dc * cell_size_m) ** 2 * kernel)
    syy = _correlate(valid, (dr * cell_size_m) ** 2 * kernel)
    sxy = _correlate(valid, -(dc * dr) * cell_size_m * cell_size_m * kernel)
    sz = _correlate(z, kernel)
    sxz = _correlate(z, kx)
    syz = _correlate(z, ky)
    szz = _correlate(z * z, kernel)

    determinant = _det3(sxx, sxy, sx, sxy, syy, sy, sx, sy, n)
    scale = np.maximum(sxx * syy * np.maximum(n, 1.0), 1.0e-30)
    solvable = dtm_valid & (n >= 3) & (np.abs(determinant) > 1.0e-9 * scale)
    safe_det = np.where(solvable, determinant, 1.0)
    coef_a = _det3(sxz, sxy, sx, syz, syy, sy, sz, sy, n) / safe_det
    coef_b = _det3(sxx, sxz, sx, sxy, syz, sy, sx, sz, n) / safe_det
    coef_c = _det3(sxx, sxy, sxz, sxy, syy, syz, sx, sy, sz) / safe_det

    gradient = np.hypot(coef_a, coef_b)
    slope = np.degrees(np.arctan(gradient))
    aspect = (np.degrees(np.arctan2(-coef_a, -coef_b)) + 360.0) % 360.0
    aspect = np.where(gradient > 1.0e-6, aspect, np.nan)

    sse = szz - coef_a * sxz - coef_b * syz - coef_c * sz
    rms = np.sqrt(np.maximum(sse, 0.0) / np.maximum(n, 1.0))
    centre_residual = np.where(solvable, np.abs(z - coef_c), 0.0)
    micro = _disk_max_filter(centre_residual, inner_px, 0.0)
    # Curved terrain: for a paraboloid the max/RMS residual ratio is ~1.7 and for
    # a sinusoid ~1.41, so 1.4 x RMS is a safe lower-bound estimate of the max
    # plane residual that the per-cell micro-relief term alone would miss.
    roughness = np.maximum(micro, ROUGHNESS_RMS_FACTOR * rms)

    slope = np.where(solvable, slope, np.nan)
    aspect = np.where(solvable, aspect, np.nan)
    roughness = np.where(solvable, roughness, np.nan)
    valid_fraction = np.where(dtm_valid, n / expected, 0.0)

    result = {
        "slope": slope,
        "aspect": aspect,
        "roughness": roughness,
        "valid_fraction": valid_fraction,
    }
    if ndsm is None or ndsm_valid is None:
        nan = np.full(dtm.shape, np.nan)
        result.update(
            tdp_obstacle=nan,
            area_obstacle=nan.copy(),
            tdp_coverage=nan.copy(),
            area_coverage=nan.copy(),
        )
        return result

    obstacle_valid = ndsm_valid.astype(np.float64)
    heights = np.where(ndsm_valid, np.maximum(ndsm.astype(np.float64), 0.0), -1.0)
    outer_kernel = disk_kernel(outer_px)
    tdp_coverage = np.clip(np.rint(_correlate(obstacle_valid, kernel)) / expected, 0.0, 1.0)
    ring_kernel = outer_kernel.copy()
    pad = (outer_kernel.shape[0] - kernel.shape[0]) // 2
    ring_kernel[pad : pad + kernel.shape[0], pad : pad + kernel.shape[1]] -= kernel
    if ring_kernel.sum() > 0:
        area_coverage = np.clip(
            np.rint(_correlate(obstacle_valid, ring_kernel)) / float(ring_kernel.sum()),
            0.0,
            1.0,
        )
    else:
        area_coverage = np.ones(dtm.shape, dtype=np.float64)
    tdp_obstacle = _disk_max_filter(heights, inner_px, -1.0)
    area_obstacle = _disk_max_filter(heights, outer_px, -1.0)
    result.update(
        tdp_obstacle=np.where(tdp_obstacle >= 0.0, tdp_obstacle, np.nan),
        area_obstacle=np.where(area_obstacle >= 0.0, area_obstacle, np.nan),
        tdp_coverage=tdp_coverage,
        area_coverage=area_coverage,
    )
    return result


def analyze_terrain(
    dtm: np.ndarray,
    dtm_valid: np.ndarray,
    dsm: np.ndarray | None,
    dsm_valid: np.ndarray | None,
    cell_size_m: float,
    tdp_radius_m: float,
    cleared_ring_m: float,
    tile_target_cells: int = 1_500_000,
    progress: ProgressCallback | None = None,
) -> TerrainResult:
    """Analyze slope, plane residuals, and obstacle heights around every cell."""

    if dtm.ndim != 2 or dtm_valid.shape != dtm.shape:
        raise ValueError("DTM and its valid mask must be matching two-dimensional arrays")
    if dsm is not None and (dsm.shape != dtm.shape or dsm_valid is None or dsm_valid.shape != dtm.shape):
        raise ValueError("DSM and its valid mask must match the DTM grid")
    if cell_size_m <= 0.0:
        raise ValueError("cell_size_m must be positive")

    rows, cols = dtm.shape
    inner_px, outer_px = radii_px(cell_size_m, tdp_radius_m, cleared_ring_m)
    # Micro-relief needs the plane of every neighbour within the TDP radius,
    # and each of those planes needs its own TDP window: halo >= 2 x inner.
    halo = int(math.ceil(max(outer_px, 2.0 * inner_px))) + 2
    tile_rows = max(16, int(tile_target_cells // max(cols, 1)))

    ndsm = None
    ndsm_valid = None
    if dsm is not None and dsm_valid is not None:
        ndsm_valid = dtm_valid & dsm_valid
        ndsm = np.where(ndsm_valid, dsm.astype(np.float32) - dtm.astype(np.float32), np.nan)

    outputs = {
        name: np.full((rows, cols), np.nan, dtype=np.float32)
        for name in (
            "slope",
            "aspect",
            "roughness",
            "valid_fraction",
            "tdp_obstacle",
            "area_obstacle",
            "tdp_coverage",
            "area_coverage",
        )
    }
    tile_count = int(math.ceil(rows / tile_rows))
    for tile_index, start in enumerate(range(0, rows, tile_rows), start=1):
        stop = min(rows, start + tile_rows)
        slab_start = max(0, start - halo)
        slab_stop = min(rows, stop + halo)
        slab = _analyze_slab(
            dtm[slab_start:slab_stop],
            dtm_valid[slab_start:slab_stop],
            None if ndsm is None else ndsm[slab_start:slab_stop],
            None if ndsm_valid is None else ndsm_valid[slab_start:slab_stop],
            cell_size_m,
            inner_px,
            outer_px,
        )
        local_start = start - slab_start
        local_stop = local_start + (stop - start)
        for name, values in slab.items():
            outputs[name][start:stop] = values[local_start:local_stop]
        if progress is not None:
            progress(f"Terrain tile {tile_index}/{tile_count} complete (rows {start}-{stop - 1})")

    return TerrainResult(
        slope_deg=outputs["slope"],
        aspect_deg=outputs["aspect"],
        roughness_m=outputs["roughness"],
        valid_fraction=outputs["valid_fraction"],
        max_tdp_obstacle_m=outputs["tdp_obstacle"],
        max_area_obstacle_m=outputs["area_obstacle"],
        tdp_obstacle_coverage=outputs["tdp_coverage"],
        area_obstacle_coverage=outputs["area_coverage"],
        inner_radius_px=inner_px,
        outer_radius_px=outer_px,
        method="fft-moment-plane-fit+rect-union-max (screening) / exact per-candidate check",
    )


def core_halo_px(cell_size_m: float, tdp_radius_m: float, cleared_ring_m: float) -> int:
    """Cells of context needed around the AOI so that terrain values and exact
    checks inside the AOI are identical to a full-raster run."""
    inner_px, outer_px = radii_px(cell_size_m, tdp_radius_m, cleared_ring_m)
    return int(math.ceil(max(outer_px, 2.0 * inner_px))) + 2


def core_window(
    candidate_mask: np.ndarray | None, shape: tuple[int, int], halo_px: int
) -> tuple[int, int, int, int]:
    """Return (row0, row1, col0, col1) of the AOI bounding box plus halo.

    Only this core window receives the expensive per-cell terrain analysis;
    the rest of the read window is used solely for approach-ray sampling.
    """
    rows, cols = shape
    if candidate_mask is None or not candidate_mask.any():
        return 0, rows, 0, cols
    row_any = np.nonzero(candidate_mask.any(axis=1))[0]
    col_any = np.nonzero(candidate_mask.any(axis=0))[0]
    return (
        max(0, int(row_any[0]) - halo_px),
        min(rows, int(row_any[-1]) + 1 + halo_px),
        max(0, int(col_any[0]) - halo_px),
        min(cols, int(col_any[-1]) + 1 + halo_px),
    )


def ground_class_raster(
    result: TerrainResult,
    dtm_valid: np.ndarray,
    max_slope_deg: float,
    max_upslope_deg: float,
    max_roughness_m: float,
    max_tdp_obstacle_m: float,
    max_ring_obstacle_m: float,
    minimum_valid_fraction: float,
    minimum_ring_coverage: float,
    obstacle_evidence: bool,
) -> np.ndarray:
    """Return a uint8 class raster.

    0 = fails ground criteria, 1 = passes for any landing heading,
    2 = passes only for an upslope landing, 255 = no data.
    """

    with np.errstate(invalid="ignore"):
        base = (
            np.isfinite(result.slope_deg)
            & np.isfinite(result.roughness_m)
            & (result.valid_fraction >= minimum_valid_fraction)
            & (result.roughness_m <= max_roughness_m)
            & (result.slope_deg <= max_upslope_deg)
        )
        if obstacle_evidence:
            base &= np.nan_to_num(result.tdp_obstacle_coverage, nan=0.0) >= minimum_valid_fraction
            base &= np.nan_to_num(result.area_obstacle_coverage, nan=0.0) >= minimum_ring_coverage
            base &= np.nan_to_num(result.max_tdp_obstacle_m, nan=np.inf) <= max_tdp_obstacle_m
            base &= np.nan_to_num(result.max_area_obstacle_m, nan=np.inf) <= max_ring_obstacle_m
        any_heading = base & (result.slope_deg <= max_slope_deg)
    classes = np.zeros(result.slope_deg.shape, dtype=np.uint8)
    classes[base] = GROUND_UPSLOPE_ONLY
    classes[any_heading] = GROUND_PASS
    classes[~dtm_valid] = GROUND_NODATA
    return classes


def exact_window_metrics(
    row: int,
    col: int,
    dtm: np.ndarray,
    dtm_valid: np.ndarray,
    ndsm: np.ndarray | None,
    ndsm_valid: np.ndarray | None,
    cell_size_m: float,
    inner_px: float,
    outer_px: float,
) -> dict[str, float | None]:
    """Exact v0.3-equivalent metrics for one candidate centre.

    Plane fit by least squares on every valid TDP cell, maximum absolute plane
    residual, and true-disk maxima of above-ground height in the TDP and in
    the cleared ring. Cells outside the raster count as missing.
    """

    rows, cols = dtm.shape
    inner_dr, inner_dc = disk_offsets(inner_px)
    outer_dr, outer_dc = disk_offsets(outer_px)
    ring_select = outer_dr * outer_dr + outer_dc * outer_dc > inner_px * inner_px + 1.0e-9
    ring_dr, ring_dc = outer_dr[ring_select], outer_dc[ring_select]

    def gather(dr: np.ndarray, dc: np.ndarray, values: np.ndarray, mask: np.ndarray):
        rr = row + dr
        cc = col + dc
        inside = (rr >= 0) & (rr < rows) & (cc >= 0) & (cc < cols)
        rr_in = rr[inside]
        cc_in = cc[inside]
        good = mask[rr_in, cc_in]
        return dr[inside][good], dc[inside][good], values[rr_in, cc_in][good]

    pdr, pdc, pz = gather(inner_dr, inner_dc, dtm, dtm_valid)
    result: dict[str, float | None] = {
        "valid_fraction": float(len(pz)) / float(len(inner_dr)),
        "slope_deg": None,
        "aspect_deg": None,
        "max_residual_m": None,
        "tdp_obstacle_m": None,
        "ring_obstacle_m": None,
        "tdp_obstacle_coverage": None,
        "ring_coverage": 1.0,
    }
    if len(pz) >= 3:
        x = pdc.astype(np.float64) * cell_size_m
        y = -pdr.astype(np.float64) * cell_size_m
        z = pz.astype(np.float64)
        design = np.column_stack([x, y, np.ones_like(x)])
        coefficients, *_ = np.linalg.lstsq(design, z - z.mean(), rcond=None)
        a, b, c = (float(value) for value in coefficients)
        residuals = np.abs((z - z.mean()) - (a * x + b * y + c))
        gradient = math.hypot(a, b)
        result["slope_deg"] = math.degrees(math.atan(gradient))
        result["aspect_deg"] = (
            (math.degrees(math.atan2(-a, -b)) + 360.0) % 360.0 if gradient > 1.0e-6 else None
        )
        result["max_residual_m"] = float(residuals.max())

    if ndsm is not None and ndsm_valid is not None:
        _, _, tdp_heights = gather(inner_dr, inner_dc, ndsm, ndsm_valid)
        result["tdp_obstacle_coverage"] = float(len(tdp_heights)) / float(len(inner_dr))
        if len(tdp_heights):
            result["tdp_obstacle_m"] = float(np.maximum(tdp_heights, 0.0).max())
        if len(ring_dr):
            _, _, ring_heights = gather(ring_dr, ring_dc, ndsm, ndsm_valid)
            result["ring_coverage"] = float(len(ring_heights)) / float(len(ring_dr))
            if len(ring_heights):
                result["ring_obstacle_m"] = float(np.maximum(ring_heights, 0.0).max())
    return result
