# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Typed data models used by the HLZ analysis engine (v0.6.7)."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

# Rating classes, ordered from best to worst. The order drives ranking.
RATING_HIGHLY_SUITABLE = "Highly suitable"
RATING_SUITABLE = "Suitable"
RATING_MARGINAL = "Marginal"
RATING_UNSUITABLE = "Unsuitable"
RATING_UNASSESSED = "Unassessed"
RATING_ORDER = (
    RATING_HIGHLY_SUITABLE,
    RATING_SUITABLE,
    RATING_MARGINAL,
    RATING_UNASSESSED,
    RATING_UNSUITABLE,
)

# Approach sector classes.
SECTOR_CLEAR = "CLEAR"
SECTOR_MARGINAL = "MARGINAL"
SECTOR_BLOCKED = "BLOCKED"
SECTOR_INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True)
class AircraftProfile:
    """Aircraft-specific physical and approach constraints."""

    profile_id: str
    display_name: str
    tdp_diameter_m: float
    cleared_ring_m: float
    max_landing_slope_deg: float
    max_upslope_landing_deg: float
    max_roughness_m: float
    tdp_obstacle_height_m: float
    ring_obstacle_height_m: float
    approach_range_m: float
    corridor_half_width_deg: float
    suitable_angle_deg: float
    unsuitable_angle_deg: float
    clearance_height_m: float
    hover_ceiling_ige_ft: float | None = None
    hover_ceiling_oge_ft: float | None = None
    description: str = ""

    @property
    def tdp_radius_m(self) -> float:
        return self.tdp_diameter_m / 2.0

    @property
    def analysis_radius_m(self) -> float:
        return self.tdp_radius_m + self.cleared_ring_m


@dataclass(frozen=True)
class MissionProfile:
    """Weights and execution controls for one mission profile."""

    profile_id: str
    display_name: str
    slope_weight: float
    roughness_weight: float
    approach_weight: float
    obstacle_weight: float
    data_quality_weight: float
    azimuth_step_deg: float = 10.0
    ray_step_deg: float = 5.0
    max_candidates: int = 1000
    max_candidates_per_tile: int = 100
    minimum_candidate_separation_factor: float = 1.0
    max_input_cells: int = 16_000_000  # terrain cells per tile (memory control, ~60 B/cell)
    max_surface_cells: int = 150_000_000  # unused since v0.6.5 (kept for profile compatibility)
    minimum_valid_fraction: float = 0.95
    minimum_ring_coverage_fraction: float = 0.90
    minimum_approach_coverage_fraction: float = 0.80
    upslope_alignment_tolerance_deg: float = 30.0
    tile_target_cells: int = 1_500_000

    @property
    def weight_total(self) -> float:
        return (
            self.slope_weight
            + self.roughness_weight
            + self.approach_weight
            + self.obstacle_weight
            + self.data_quality_weight
        )


@dataclass(frozen=True)
class DoctrineProfile:
    """Doctrine provenance and decision thresholds."""

    profile_id: str
    display_name: str
    source_title: str
    source_edition: str
    approval_status: str
    highly_suitable_score: float = 80.0
    suitable_score: float = 60.0
    marginal_score: float = 40.0

    @property
    def is_approved(self) -> bool:
        return self.approval_status.strip().lower() == "approved"


@dataclass(frozen=True)
class GridGeometry:
    """Minimal north-up raster geometry independent of any GIS product.

    ``x_origin``/``y_origin`` are the coordinates of the upper-left corner of
    the upper-left cell. ``pixel_height`` is negative for north-up rasters.
    """

    x_origin: float
    y_origin: float
    pixel_width: float
    pixel_height: float
    width: int
    height: int

    def xy(self, row: float, col: float) -> tuple[float, float]:
        """Return the cell-centre coordinates of (row, col)."""
        return (
            self.x_origin + (col + 0.5) * self.pixel_width,
            self.y_origin + (row + 0.5) * self.pixel_height,
        )

    def row_col(self, x: float, y: float) -> tuple[int, int]:
        """Return the (row, col) of the cell containing (x, y).

        ``math.floor`` is used so that points west of / above the origin map
        to negative indices instead of being truncated into row/column 0.
        """
        col = int(math.floor((x - self.x_origin) / self.pixel_width))
        row = int(math.floor((y - self.y_origin) / self.pixel_height))
        return row, col

    @property
    def cell_size_m(self) -> float:
        return (abs(self.pixel_width) + abs(self.pixel_height)) / 2.0

    @property
    def x_min(self) -> float:
        return self.x_origin

    @property
    def y_max(self) -> float:
        return self.y_origin

    @property
    def x_max(self) -> float:
        return self.x_origin + self.width * self.pixel_width

    @property
    def y_min(self) -> float:
        return self.y_origin + self.height * self.pixel_height


@dataclass
class ApproachSector:
    """One evaluated approach/departure sector.

    ``azimuth_deg`` is the true bearing FROM the landing point TOWARD the
    sector (i.e. the direction the aircraft comes from on final). The landing
    heading flown on final is therefore ``(azimuth_deg + 180) % 360``.
    """

    azimuth_deg: float
    worst_angle_deg: float
    classification: str
    coverage_fraction: float = 0.0
    worst_x: float | None = None
    worst_y: float | None = None
    worst_distance_m: float | None = None
    worst_height_above_lz_m: float | None = None
    worst_obstacle: str = ""  # v0.6.9: set when the limiting obstacle is one from the obstacle list

    @property
    def landing_heading_deg(self) -> float:
        return (self.azimuth_deg + 180.0) % 360.0

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["landing_heading_deg"] = self.landing_heading_deg
        return result


@dataclass
class Candidate:
    """One HLZ candidate and its evidence."""

    candidate_id: str
    row: int
    col: int
    x: float
    y: float
    elevation_m: float
    slope_deg: float
    aspect_deg: float | None
    roughness_m: float
    max_tdp_obstacle_m: float | None
    max_ring_obstacle_m: float | None
    ring_coverage_fraction: float
    component_area_m2: float
    rank: int = 0
    selection_quality: float = 0.0
    excluded_distance_m: float | None = None
    nearest_obstacle_m: float | None = None  # v0.6.9: distance to the nearest listed obstacle (tower, wire, ...)
    nearest_obstacle_text: str = ""
    search_distance_m: float | None = None
    search_bearing_deg: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    mgrs: str = ""
    upslope_only: bool = False
    landing_heading_deg: float | None = None
    density_altitude_ft: float | None = None
    best_approach_azimuth_deg: float | None = None
    best_approach_angle_deg: float | None = None
    clear_sector_count: int = 0
    assessed_sector_count: int = 0
    opposing_corridors: bool = False
    approach_basis: str = "NONE"
    score: float = 0.0
    slope_component: float = 0.0
    roughness_component: float = 0.0
    approach_component: float = 0.0
    obstacle_component: float = 0.0
    data_quality_component: float = 0.0
    rating: str = RATING_UNASSESSED
    status: str = "SCREENING"
    confidence: str = "SCREENING"
    limiting_factor: str = "Not scored"
    notes: str = ""
    sectors: list[ApproachSector] = field(default_factory=list)

    def to_dict(self, include_sectors: bool = False) -> dict[str, Any]:
        result = asdict(self)
        result.pop("sectors", None)
        if include_sectors:
            result["sectors"] = [sector.to_dict() for sector in self.sectors]
        return result
