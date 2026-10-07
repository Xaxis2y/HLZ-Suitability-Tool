# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Swept-sector approach and departure obstacle analysis (v0.6.7).

Improvements over v0.3:

* Fully vectorised: every ray around a candidate is sampled in one NumPy
  operation, and each ray is evaluated once and re-used by every overlapping
  sector (v0.3 re-sampled the same rays for each sector in pure Python).
* Coverage-aware: rays that leave the raster or cross no-data are counted.
  A sector whose coverage is below the mission threshold is labelled
  ``INCOMPLETE`` instead of silently appearing clear near the AOI edge.
* Terrain-only mode: when no DSM is supplied the bare-earth DTM is still used
  to detect terrain masking of the approach (clearly flagged as such).
"""

from __future__ import annotations

import math

import numpy as np

from .obstacles import VerticalSamples
from .models import (
    SECTOR_BLOCKED,
    SECTOR_CLEAR,
    SECTOR_INCOMPLETE,
    SECTOR_MARGINAL,
    AircraftProfile,
    ApproachSector,
    Candidate,
    GridGeometry,
    MissionProfile,
)


def angular_difference(first: float | np.ndarray, second: float | np.ndarray):  # type: ignore[no-untyped-def]
    """Smallest absolute difference between two bearings, in degrees."""
    return np.abs((np.asarray(first) - np.asarray(second) + 180.0) % 360.0 - 180.0)


def build_surface(
    dtm: np.ndarray,
    dtm_valid: np.ndarray,
    dsm: np.ndarray | None,
    dsm_valid: np.ndarray | None,
) -> np.ndarray:
    """Return the obstacle surface used for approach analysis (NaN = unknown)."""
    terrain = np.where(dtm_valid, dtm.astype(np.float32), np.nan)
    if dsm is None or dsm_valid is None:
        return terrain
    return np.where(dsm_valid, dsm.astype(np.float32), terrain)


def analyze_candidate_approaches(
    candidate: Candidate,
    surface: np.ndarray,
    geometry: GridGeometry,
    aircraft: AircraftProfile,
    mission: MissionProfile,
    vertical: VerticalSamples | None = None,
) -> list[ApproachSector]:
    """Evaluate overlapping swept sectors around one candidate.

    ``vertical`` holds listed obstacles (towers, masts, wires). Each is tested exactly against every sector it
    lies in, because a thin structure can fall between the rays (v0.6.9).
    """

    if surface.shape != (geometry.height, geometry.width):
        raise ValueError("Approach surface must match the grid geometry")

    distance_step_m = max(geometry.cell_size_m, 0.5)
    first_distance_m = max(aircraft.tdp_radius_m, distance_step_m)
    distances = np.arange(
        first_distance_m,
        aircraft.approach_range_m + 0.5 * distance_step_m,
        distance_step_m,
        dtype=np.float64,
    )
    ray_count = int(round(360.0 / mission.ray_step_deg))
    ray_azimuths = np.arange(ray_count, dtype=np.float64) * mission.ray_step_deg
    radians = np.radians(ray_azimuths)[:, None]
    xs = candidate.x + np.sin(radians) * distances[None, :]
    ys = candidate.y + np.cos(radians) * distances[None, :]
    cols = np.floor((xs - geometry.x_origin) / geometry.pixel_width).astype(np.int64)
    rows = np.floor((ys - geometry.y_origin) / geometry.pixel_height).astype(np.int64)
    inside = (rows >= 0) & (rows < geometry.height) & (cols >= 0) & (cols < geometry.width)

    samples = np.full(xs.shape, np.nan, dtype=np.float64)
    samples[inside] = surface[rows[inside], cols[inside]]
    observed = np.isfinite(samples)
    reference = candidate.elevation_m + aircraft.clearance_height_m
    with np.errstate(invalid="ignore"):
        angles = np.degrees(np.arctan2(samples - reference, distances[None, :]))
    angles_filled = np.where(observed, angles, -np.inf)
    worst_index = np.argmax(angles_filled, axis=1)
    ray_worst = angles_filled[np.arange(ray_count), worst_index]
    ray_coverage = observed.mean(axis=1)

    sectors: list[ApproachSector] = []
    tolerance = 1.0e-9
    centres = np.arange(0.0, 360.0, mission.azimuth_step_deg)
    listed = (
        vertical.sector_angles(
            candidate.x, candidate.y, reference, first_distance_m, aircraft.approach_range_m, centres,
            aircraft.corridor_half_width_deg,
        )
        if vertical is not None
        else [None] * len(centres)
    )
    for centre_index, centre in enumerate(centres):
        hit = listed[centre_index]
        member = angular_difference(ray_azimuths, centre) <= aircraft.corridor_half_width_deg + tolerance
        member_indices = np.nonzero(member)[0]
        coverage = float(ray_coverage[member_indices].mean()) if len(member_indices) else 0.0
        member_worst = ray_worst[member_indices]
        have_rays = len(member_indices) > 0 and bool(np.isfinite(member_worst).any())
        worst_x = worst_y = worst_distance = worst_height = None
        worst_angle = math.nan
        obstacle_text = ""
        if have_rays:
            local = int(np.argmax(member_worst))
            ray_index = int(member_indices[local])
            sample_index = int(worst_index[ray_index])
            worst_angle = float(member_worst[local])
            worst_x = float(xs[ray_index, sample_index])
            worst_y = float(ys[ray_index, sample_index])
            worst_distance = float(distances[sample_index])
            worst_height = float(samples[ray_index, sample_index] - candidate.elevation_m)
        if hit is not None and (not have_rays or hit[0] > worst_angle):
            # A listed obstacle (tower, mast, wire) is steeper than anything the rays saw, or the rays saw nothing.
            worst_angle, worst_x, worst_y, worst_distance, obstacle_text, top_z = hit[0], hit[1], hit[2], hit[3], hit[4], hit[5]
            worst_height = float(top_z - candidate.elevation_m)
        elif hit is not None and hit[0] >= worst_angle - 0.5:
            obstacle_text = hit[4]  # the rays saw the same obstacle (it is also in the surface model)
        if not have_rays and hit is None:
            sectors.append(
                ApproachSector(
                    azimuth_deg=float(centre),
                    worst_angle_deg=math.nan,
                    classification=SECTOR_INCOMPLETE,
                    coverage_fraction=coverage,
                )
            )
            continue
        if coverage < mission.minimum_approach_coverage_fraction:
            classification = SECTOR_INCOMPLETE
        elif worst_angle <= aircraft.suitable_angle_deg:
            classification = SECTOR_CLEAR
        elif worst_angle <= aircraft.unsuitable_angle_deg:
            classification = SECTOR_MARGINAL
        else:
            classification = SECTOR_BLOCKED
        # A sector that is already blocked by observed data stays blocked even
        # if coverage is incomplete: missing data cannot make it clearer.
        if coverage < mission.minimum_approach_coverage_fraction and worst_angle > aircraft.unsuitable_angle_deg:
            classification = SECTOR_BLOCKED
        sectors.append(
            ApproachSector(
                azimuth_deg=float(centre),
                worst_angle_deg=worst_angle,
                classification=classification,
                coverage_fraction=round(coverage, 3),
                worst_x=worst_x,
                worst_y=worst_y,
                worst_distance_m=worst_distance,
                worst_height_above_lz_m=worst_height,
                worst_obstacle=obstacle_text,
            )
        )
    return sectors


def attach_approach_summary(
    candidate: Candidate,
    sectors: list[ApproachSector],
    mission: MissionProfile,
    basis: str,
) -> None:
    """Attach best-sector, landing-heading and opposing-corridor evidence."""

    candidate.sectors = sectors
    candidate.approach_basis = basis
    assessed = [
        sector
        for sector in sectors
        if sector.classification != SECTOR_INCOMPLETE and math.isfinite(sector.worst_angle_deg)
    ]
    clear = [sector for sector in assessed if sector.classification == SECTOR_CLEAR]
    candidate.assessed_sector_count = len(assessed)
    candidate.clear_sector_count = len(clear)

    usable = assessed
    if candidate.upslope_only and candidate.aspect_deg is not None:
        # Upslope landing: the aircraft must land facing uphill. The uphill
        # direction is the aspect (downslope bearing) + 180. The approach
        # comes FROM the opposite side, i.e. from the downslope bearing.
        upslope_heading = (candidate.aspect_deg + 180.0) % 360.0
        usable = [
            sector
            for sector in assessed
            if float(angular_difference(sector.landing_heading_deg, upslope_heading))
            <= mission.upslope_alignment_tolerance_deg
        ]

    if usable:
        best = min(usable, key=lambda sector: (sector.worst_angle_deg, sector.azimuth_deg))
        candidate.best_approach_azimuth_deg = best.azimuth_deg
        candidate.best_approach_angle_deg = round(best.worst_angle_deg, 2)
        candidate.landing_heading_deg = best.landing_heading_deg
    else:
        candidate.best_approach_azimuth_deg = None
        candidate.best_approach_angle_deg = None
        candidate.landing_heading_deg = None

    candidate.opposing_corridors = any(
        abs(float(angular_difference(first.azimuth_deg, second.azimuth_deg)) - 180.0) <= 10.0
        for index, first in enumerate(clear)
        for second in clear[index + 1 :]
    )
