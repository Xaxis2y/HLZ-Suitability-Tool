# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Candidate-centre extraction, exact verification and spatial suppression.

v0.6.7 changes:

* Candidates are ranked by a quality surface that also rewards distance from
  the edge of the passing area (centres in the middle of a field are more
  robust to DEM error than centres hugging a tree line).
* Each candidate is re-checked with the exact window metrics before it is
  accepted (see ``terrain.exact_window_metrics``).
* Spatial suppression uses a blocked-cell raster (O(1) look-up) instead of the
  O(N x selected) Python distance loop used in v0.3.
* Candidates can be limited to an AOI mask while the analysis window extends
  beyond the AOI, so approach rays near the AOI edge still see real data.
"""

from __future__ import annotations

import math
from typing import Callable

import numpy as np
from scipy import ndimage

from .models import AircraftProfile, Candidate, GridGeometry, MissionProfile
from .terrain import (
    GROUND_PASS,
    GROUND_UPSLOPE_ONLY,
    TerrainResult,
    disk_offsets,
    exact_window_metrics,
)


def quality_surface(
    ground_classes: np.ndarray,
    terrain: TerrainResult,
    aircraft: AircraftProfile,
    cell_size_m: float,
    obstacle_evidence: bool,
) -> np.ndarray:
    """Return a 0..1 quality surface (-inf where the ground test fails)."""
    passing = (ground_classes == GROUND_PASS) | (ground_classes == GROUND_UPSLOPE_ONLY)
    with np.errstate(invalid="ignore"):
        slope_quality = np.clip(1.0 - terrain.slope_deg / aircraft.max_upslope_landing_deg, 0.0, 1.0)
        roughness_quality = np.clip(1.0 - terrain.roughness_m / aircraft.max_roughness_m, 0.0, 1.0)
        if obstacle_evidence:
            obstacle_quality = np.clip(
                1.0
                - np.nan_to_num(terrain.max_area_obstacle_m, nan=aircraft.ring_obstacle_height_m)
                / max(aircraft.ring_obstacle_height_m, 1.0e-6),
                0.0,
                1.0,
            )
        else:
            obstacle_quality = np.zeros(terrain.slope_deg.shape, dtype=np.float32)
    distance_m = ndimage.distance_transform_edt(passing) * cell_size_m
    edge_quality = np.clip(distance_m / max(aircraft.tdp_radius_m, cell_size_m), 0.0, 1.0)
    quality = (
        0.35 * slope_quality
        + 0.25 * roughness_quality
        + 0.15 * obstacle_quality
        + 0.25 * edge_quality
    )
    # Upslope-only centres rank after any-heading centres of equal quality.
    quality = np.where(ground_classes == GROUND_UPSLOPE_ONLY, quality - 0.5, quality)
    return np.where(passing, quality, -np.inf)


def extract_candidates(
    ground_classes: np.ndarray,
    terrain: TerrainResult,
    dtm: np.ndarray,
    dtm_valid: np.ndarray,
    ndsm: np.ndarray | None,
    ndsm_valid: np.ndarray | None,
    geometry: GridGeometry,
    aircraft: AircraftProfile,
    mission: MissionProfile,
    candidate_mask: np.ndarray | None = None,
    log: Callable[[str], None] | None = None,
) -> tuple[list[Candidate], dict[str, int]]:
    """Choose high-quality, spatially separated, exactly verified centres."""

    if ground_classes.shape != dtm.shape:
        raise ValueError("Candidate inputs must use the same grid")
    obstacle_evidence = ndsm is not None
    cell = geometry.cell_size_m
    passing = (ground_classes == GROUND_PASS) | (ground_classes == GROUND_UPSLOPE_ONLY)
    if candidate_mask is not None:
        passing &= candidate_mask
    stats = {"ground_pass_cells": int(passing.sum()), "exact_checks": 0, "exact_rejections": 0}
    if not passing.any():
        return [], stats

    labels, _ = ndimage.label(passing, structure=np.ones((3, 3)))
    component_sizes = np.bincount(labels.ravel())
    quality = quality_surface(ground_classes, terrain, aircraft, cell, obstacle_evidence)
    quality = np.where(passing, quality, -np.inf)

    flat = quality.ravel()
    order = np.argsort(-flat, kind="stable")
    order = order[np.isfinite(flat[order])]

    separation_m = aircraft.tdp_diameter_m * mission.minimum_candidate_separation_factor
    separation_px = separation_m / cell
    block_dr, block_dc = disk_offsets(max(separation_px - 1.0e-6, 0.0))
    # After an exact-check failure the immediate neighbourhood almost always
    # fails too; skipping it keeps the exact-check budget for new areas.
    fail_dr, fail_dc = disk_offsets(max(1.0, 0.25 * aircraft.tdp_radius_m / cell))
    blocked = np.zeros(dtm.shape, dtype=bool)
    rows_total, cols_total = dtm.shape

    selected: list[Candidate] = []
    max_exact_checks = max(50, 25 * mission.max_candidates)
    for flat_index in order:
        row, col = divmod(int(flat_index), cols_total)
        if blocked[row, col]:
            continue
        if stats["exact_checks"] >= max_exact_checks:
            if log:
                log(
                    f"Exact-check budget of {max_exact_checks} reached; "
                    "remaining centres were not examined"
                )
            break
        stats["exact_checks"] += 1
        metrics = exact_window_metrics(
            row,
            col,
            dtm,
            dtm_valid,
            ndsm,
            ndsm_valid,
            cell,
            terrain.inner_radius_px,
            terrain.outer_radius_px,
        )
        if not _passes_exact(metrics, aircraft, mission, obstacle_evidence):
            stats["exact_rejections"] += 1
            rr = row + fail_dr
            cc = col + fail_dc
            keep = (rr >= 0) & (rr < rows_total) & (cc >= 0) & (cc < cols_total)
            blocked[rr[keep], cc[keep]] = True
            continue

        x, y = geometry.xy(row, col)
        slope = float(metrics["slope_deg"])  # type: ignore[arg-type]
        component_label = int(labels[row, col])
        selected.append(
            Candidate(
                candidate_id=f"C{len(selected) + 1:04d}",
                row=row,
                col=col,
                x=x,
                y=y,
                elevation_m=round(float(dtm[row, col]), 2),
                slope_deg=round(slope, 2),
                aspect_deg=(
                    None
                    if metrics["aspect_deg"] is None
                    else round(float(metrics["aspect_deg"]), 1)  # type: ignore[arg-type]
                ),
                roughness_m=round(float(metrics["max_residual_m"]), 3),  # type: ignore[arg-type]
                max_tdp_obstacle_m=_round_or_none(metrics["tdp_obstacle_m"]),
                max_ring_obstacle_m=_round_or_none(metrics["ring_obstacle_m"]),
                ring_coverage_fraction=round(float(metrics["ring_coverage"] or 0.0), 3),
                component_area_m2=float(component_sizes[component_label])
                * abs(geometry.pixel_width * geometry.pixel_height),
                upslope_only=slope > aircraft.max_landing_slope_deg,
                selection_quality=round(float(quality[row, col]), 4),
            )
        )
        rr = row + block_dr
        cc = col + block_dc
        keep = (rr >= 0) & (rr < rows_total) & (cc >= 0) & (cc < cols_total)
        blocked[rr[keep], cc[keep]] = True
        if len(selected) >= mission.max_candidates:
            break
    return selected, stats


def _round_or_none(value: float | None, digits: int = 2) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), digits)


def _passes_exact(
    metrics: dict[str, float | None],
    aircraft: AircraftProfile,
    mission: MissionProfile,
    obstacle_evidence: bool,
) -> bool:
    if metrics["slope_deg"] is None or metrics["max_residual_m"] is None:
        return False
    if float(metrics["valid_fraction"] or 0.0) < mission.minimum_valid_fraction:  # type: ignore[arg-type]
        return False
    if float(metrics["slope_deg"]) > aircraft.max_upslope_landing_deg:  # type: ignore[arg-type]
        return False
    if float(metrics["max_residual_m"]) > aircraft.max_roughness_m:  # type: ignore[arg-type]
        return False
    if obstacle_evidence:
        coverage = metrics["tdp_obstacle_coverage"]
        if coverage is None or float(coverage) < mission.minimum_valid_fraction:
            return False
        if aircraft.cleared_ring_m > 0.0 and float(metrics["ring_coverage"] or 0.0) < (
            mission.minimum_ring_coverage_fraction
        ):
            return False
        tdp = metrics["tdp_obstacle_m"]
        if tdp is None or float(tdp) > aircraft.tdp_obstacle_height_m:
            return False
        ring = metrics["ring_obstacle_m"]
        if ring is not None and float(ring) > aircraft.ring_obstacle_height_m:
            return False
    return True
