# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""End-to-end HLZ pipeline shared by the ArcGIS Pro toolbox and the CLI.

v0.6.7: out-of-core tiled processing. The analysis area (AOI, map view or the
whole DTM) is split into tiles. For each tile only the tile, a small terrain
halo and the approach buffer are read into memory, so there is no limit on
the total area - a whole city at 1 m works. Each finished tile is
checkpointed, so an interrupted run resumes where it stopped.

GIS access is delegated to two small interfaces:

* ``TileReader``   - reads windows of the DTM/DSM and the AOI mask
                     (``io_arcpy.ArcpyTileReader``, ``io_rasterio.RasterioTileReader``,
                     or ``ArrayTileReader`` for in-memory arrays)
* ``OutputWriter`` - writes raster tiles, final rasters and vector layers
"""

from __future__ import annotations

import logging
import math
import platform
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import numpy as np

from . import __version__
from .approach import analyze_candidate_approaches, attach_approach_summary, build_surface
from .candidates import extract_candidates
from .config import check_profile_compatibility
from .geo import latlon_to_mgrs
from .masks import distance_to_mask_m
from .overview import (
    OverviewAccumulator,
    OverviewPlan,
    attach_candidates,
    finalize as finalize_overview,
    make_plan as make_overview_plan,
    tile_partial as overview_tile_partial,
    validate_cell_m as validate_overview_cell_m,
)
from .models import AircraftProfile, Candidate, DoctrineProfile, GridGeometry, MissionProfile
from .performance import density_altitude_ft
from .report import (
    console_summary,
    write_candidate_csv,
    write_html_report,
    write_metadata,
)
from .scoring import rank_candidates, rating_counts, score_candidate
from .search import SearchArea, combine_masks, intersect_windows
from .terrain import (
    GROUND_FAIL,
    GROUND_NODATA,
    GROUND_PASS,
    GROUND_UPSLOPE_ONLY,
    analyze_terrain,
    core_halo_px,
    ground_class_raster,
)
from .tiling import (
    Tile,
    TileCheckpoint,
    plan_tiles,
    run_signature,
    suppress_across_tiles,
    tile_size_px,
)

LOGGER = logging.getLogger("hlz")

# Elevations outside this range (metres) are treated as NoData. This catches
# undeclared sentinels such as -9999, -32767 or the file-geodatabase float
# NoData value (about -3.4e38) that would otherwise be read as real terrain.
PLAUSIBLE_ELEVATION_M = (-1000.0, 9000.0)

# Above this many analysed cells, AUTO mode skips the large float diagnostic
# rasters (slope, roughness, obstacle height); Ground_Class is always written.
AUTO_DIAGNOSTIC_LIMIT_CELLS = 100_000_000

# Open ground should have DSM minus DTM close to zero. A lowest-5% height above ground beyond this
# (in either direction) means the two rasters do not share a vertical datum or registration.
DSM_DTM_TOLERANCE_M = 0.5

# Rough throughput used for the run-time estimate shown before a run.
SECONDS_PER_MILLION_CELLS = (2.0, 5.0)


def plausible_elevation_mask(
    values: np.ndarray, valid: np.ndarray, label: str, notes: list[str]
) -> np.ndarray:
    """Return ``valid`` minus cells with impossible elevations; record a note."""
    low, high = PLAUSIBLE_ELEVATION_M
    with np.errstate(invalid="ignore"):
        implausible = valid & ((values < low) | (values > high))
    count = int(implausible.sum())
    if count:
        notes.append(
            f"{label}: {count:,} cells with elevations outside {low:,.0f}..{high:,.0f} m were treated "
            "as NoData (undeclared NoData value or wrong Z units?)"
        )
    return valid & ~implausible


class AnalysisError(RuntimeError):
    """Raised for invalid data or an analysis that cannot be completed."""


class AnalysisCancelled(RuntimeError):
    """Raised when the user cancels; finished tiles stay checkpointed."""


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------


class TileReader(Protocol):
    """Windowed access to the inputs (full-raster row/column coordinates)."""

    geometry: GridGeometry  # the full DTM grid
    window: tuple[int, int, int, int]  # analysis window (row0, row1, col0, col1)
    has_dsm: bool
    crs_description: str
    dtm_source: str
    dsm_source: str | None
    aoi_digest: str
    notes: list[str]

    def read(
        self, row0: int, row1: int, col0: int, col1: int
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray | None, np.ndarray | None]: ...

    def aoi_mask(self, row0: int, row1: int, col0: int, col1: int) -> np.ndarray | None:
        """None = every cell is inside the AOI; otherwise a boolean array."""
        ...


class OutputWriter(Protocol):
    """Interface implemented by GIS output adapters."""

    def begin_rasters(self, grid: GridGeometry, specs: list[tuple[str, str, float, str]], resume: bool) -> None: ...

    def write_raster_tile(self, name: str, array: np.ndarray, tile_geometry: GridGeometry, tile: Tile) -> None: ...

    def finish_rasters(self) -> dict[str, str]: ...

    def write_vectors(
        self, candidates: list[Candidate], aircraft: AircraftProfile, top_sectors: int
    ) -> dict[str, str]: ...

    def to_latlon(self, xs: list[float], ys: list[float]) -> tuple[list[float], list[float]] | None: ...


@dataclass
class InputData:
    """Arrays and geometry held in memory (small areas, tests)."""

    dtm: np.ndarray
    dtm_valid: np.ndarray
    geometry: GridGeometry
    dsm: np.ndarray | None = None
    dsm_valid: np.ndarray | None = None
    candidate_mask: np.ndarray | None = None
    exclusion_mask: np.ndarray | None = None
    obstacles: Any = None  # ObstacleSet (towers, masts, wires) or None
    search_centres: list[tuple[float, float]] | None = None
    search_radius_m: float | None = None
    crs_description: str = ""
    dtm_source: str = ""
    dsm_source: str | None = None
    notes: list[str] = field(default_factory=list)


class ArrayTileReader:
    """``TileReader`` over in-memory arrays (tests and small CLI runs)."""

    def __init__(self, data: InputData):
        self.data = data
        self.geometry = data.geometry
        self.has_dsm = data.dsm is not None
        self.crs_description = data.crs_description
        self.dtm_source = data.dtm_source
        self.dsm_source = data.dsm_source
        self.notes = list(data.notes)
        self.search_area = (
            SearchArea(data.search_centres, data.search_radius_m) if data.search_centres and data.search_radius_m else None
        )
        self.obstacles = data.obstacles
        self.obstacle_digest = data.obstacles.digest() if data.obstacles else "none"
        self.obstacle_description = data.obstacles.describe() if data.obstacles else "none"
        self.has_exclusion = data.exclusion_mask is not None
        self.exclusion_digest = (
            f"mask:{int(data.exclusion_mask.sum())}" if data.exclusion_mask is not None else "none"
        )
        self.exclusion_description = "in-memory mask" if self.has_exclusion else "none"
        rows, cols = data.dtm.shape
        if data.candidate_mask is not None and data.candidate_mask.any():
            row_any = np.nonzero(data.candidate_mask.any(axis=1))[0]
            col_any = np.nonzero(data.candidate_mask.any(axis=0))[0]
            self.window = (int(row_any[0]), int(row_any[-1]) + 1, int(col_any[0]), int(col_any[-1]) + 1)
            self.aoi_digest = f"mask:{int(data.candidate_mask.sum())}:{self.window}"
        else:
            self.window = (0, rows, 0, cols)
            self.aoi_digest = "none"
        if self.search_area is not None:
            circle_window = self.search_area.window(self.geometry)
            combined = intersect_windows(self.window, circle_window) if circle_window else None
            if combined is None:
                raise AnalysisError("The search circle does not overlap the data (or the area of interest).")
            self.window = combined
            self.aoi_digest = f"{self.aoi_digest}|{self.search_area.digest}"

    def read(self, row0: int, row1: int, col0: int, col1: int):  # type: ignore[no-untyped-def]
        data = self.data
        dsm = None if data.dsm is None else data.dsm[row0:row1, col0:col1]
        dsm_valid = None if data.dsm_valid is None else data.dsm_valid[row0:row1, col0:col1]
        return (
            data.dtm[row0:row1, col0:col1],
            data.dtm_valid[row0:row1, col0:col1],
            dsm,
            dsm_valid,
        )

    def exclusion_mask(self, row0: int, row1: int, col0: int, col1: int) -> np.ndarray | None:
        if self.data.exclusion_mask is None:
            return None
        return self.data.exclusion_mask[row0:row1, col0:col1]

    def aoi_mask(self, row0: int, row1: int, col0: int, col1: int) -> np.ndarray | None:
        polygon = None if self.data.candidate_mask is None else self.data.candidate_mask[row0:row1, col0:col1]
        if self.search_area is None:
            return polygon
        circle = self.search_area.mask(_sub_geometry(self.geometry, row0, row1, col0, col1))
        return combine_masks((row1 - row0, col1 - col0), polygon, circle)


@dataclass
class RunOptions:
    run_name: str = "HLZ_run"
    oat_c: float | None = None
    altimeter_inhg: float = 29.92
    performance_mode: str = "OGE"
    max_candidates_override: int | None = None
    report_top_n: int = 10
    diagnostic_rasters: str = "AUTO"  # AUTO | ALWAYS | NEVER
    report_basemap: str = "ONLINE"  # ONLINE | OFFLINE (HTML report map tiles)
    overview_cell_m: float | None = None  # v0.7.0: terrain overview grid cell size in metres (None or 0 = no overview)


@dataclass
class RunResult:
    candidates: list[Candidate]
    metadata: dict[str, Any]
    outputs: dict[str, str]
    summary_lines: list[str]


# ---------------------------------------------------------------------------
# Planning helpers (also used by the toolbox dialog)
# ---------------------------------------------------------------------------


def buffer_px_for(aircraft: AircraftProfile, cell_size_m: float) -> int:
    """Approach buffer read around each tile, in cells."""
    return int(math.ceil((aircraft.approach_range_m + aircraft.analysis_radius_m) / cell_size_m)) + 2


def plan_for(
    window: tuple[int, int, int, int], cell_size_m: float, aircraft: AircraftProfile, mission: MissionProfile
) -> tuple[list[Tile], int, int]:
    """Return (tiles, halo_px, tile_px) for an analysis window."""
    halo = core_halo_px(cell_size_m, aircraft.tdp_radius_m, aircraft.cleared_ring_m)
    tile_px = tile_size_px(mission.max_input_cells, halo)
    return plan_tiles(window, tile_px), halo, tile_px


def estimate_runtime_text(analysed_cells: int, tile_count: int) -> str:
    low, high = SECONDS_PER_MILLION_CELLS
    seconds = (analysed_cells / 1.0e6 * low, analysed_cells / 1.0e6 * high)

    def fmt(value: float) -> str:
        if value < 90:
            return f"{value:.0f} s"
        if value < 5400:
            return f"{value / 60:.0f} min"
        return f"{value / 3600:.1f} h"

    return f"{tile_count} tile(s), roughly {fmt(seconds[0])}-{fmt(seconds[1])}"


def _sub_geometry(geometry: GridGeometry, row0: int, row1: int, col0: int, col1: int) -> GridGeometry:
    return GridGeometry(
        x_origin=geometry.x_origin + col0 * geometry.pixel_width,
        y_origin=geometry.y_origin + row0 * geometry.pixel_height,
        pixel_width=geometry.pixel_width,
        pixel_height=geometry.pixel_height,
        width=col1 - col0,
        height=row1 - row0,
    )


# ---------------------------------------------------------------------------
# Per-tile analysis (pure NumPy; ready for parallel execution)
# ---------------------------------------------------------------------------


@dataclass
class TileOutput:
    candidates: list[Candidate]
    rasters: dict[str, np.ndarray]
    stats: dict[str, Any]


def analyze_tile(
    tile: Tile,
    reader: TileReader,
    aircraft: AircraftProfile,
    mission: MissionProfile,
    doctrine: DoctrineProfile,
    options: RunOptions,
    halo: int,
    buffer_px: int,
    want_diagnostics: bool,
    overview_plan: OverviewPlan | None = None,
) -> TileOutput:
    geometry = reader.geometry
    cell = geometry.cell_size_m
    stats: dict[str, Any] = {
        "valid_cells": 0,
        "ground_pass_cells": 0,
        "exact_checks": 0,
        "exact_rejections": 0,
        "candidates": 0,
        "incomplete_sectors": 0,
        "total_sectors": 0,
        "excluded_cells": 0,
        "notes": [],
    }
    aoi = reader.aoi_mask(tile.row0, tile.row1, tile.col0, tile.col1)
    if aoi is not None and not aoi.any():
        stats["skipped"] = "outside AOI"
        return TileOutput([], {}, stats)

    # Read window = tile + approach buffer; core = tile + terrain halo.
    read_r0 = max(0, tile.row0 - buffer_px)
    read_r1 = min(geometry.height, tile.row1 + buffer_px)
    read_c0 = max(0, tile.col0 - buffer_px)
    read_c1 = min(geometry.width, tile.col1 + buffer_px)
    dtm, dtm_valid, dsm, dsm_valid = reader.read(read_r0, read_r1, read_c0, read_c1)
    notes: list[str] = []
    dtm_valid = plausible_elevation_mask(dtm, dtm_valid, "DTM", notes)
    if dsm is not None and dsm_valid is not None:
        dsm_valid = plausible_elevation_mask(dsm, dsm_valid, "DSM", notes) & dtm_valid
    stats["notes"] = notes
    read_geometry = _sub_geometry(geometry, read_r0, read_r1, read_c0, read_c1)

    # Listed obstacles (towers, masts, wires, buildings): burn their footprints into the surface as ground + AGL,
    # and keep their points for the exact approach test (a thin tower can fall between two rays).
    obstacle_set = getattr(reader, "obstacles", None)
    obstacle_height = None
    vertical = None
    if obstacle_set:
        obstacle_height = obstacle_set.height_raster(read_geometry)
        if not obstacle_height.any():
            obstacle_height = None
        elif dsm is not None and dsm_valid is not None:
            footprint = (obstacle_height > 0) & dtm_valid
            top = (dtm + obstacle_height).astype(dsm.dtype)
            dsm = np.where(footprint, np.maximum(np.where(dsm_valid, dsm, top), top), dsm)
            dsm_valid = dsm_valid | footprint
        vertical = obstacle_set.vertical_samples(read_geometry, dtm, dtm_valid)
        stats["obstacle_points"] = 0 if vertical is None else len(vertical)

    core_r0 = max(read_r0, tile.row0 - halo)
    core_r1 = min(read_r1, tile.row1 + halo)
    core_c0 = max(read_c0, tile.col0 - halo)
    core_c1 = min(read_c1, tile.col1 + halo)
    cs = (slice(core_r0 - read_r0, core_r1 - read_r0), slice(core_c0 - read_c0, core_c1 - read_c0))
    core_geometry = _sub_geometry(geometry, core_r0, core_r1, core_c0, core_c1)
    core_dtm = dtm[cs]
    core_dtm_valid = dtm_valid[cs]
    core_dsm = None if dsm is None else dsm[cs]
    core_dsm_valid = None if dsm_valid is None else dsm_valid[cs]

    # Tile region inside the core arrays, and the candidate mask (tile x AOI).
    ts = (slice(tile.row0 - core_r0, tile.row1 - core_r0), slice(tile.col0 - core_c0, tile.col1 - core_c0))
    candidate_mask = np.zeros(core_dtm.shape, dtype=bool)
    candidate_mask[ts] = True if aoi is None else aoi
    stats["valid_cells"] = int(core_dtm_valid[ts].sum())

    obstacle_evidence = core_dsm is not None
    terrain = analyze_terrain(
        dtm=core_dtm,
        dtm_valid=core_dtm_valid,
        dsm=core_dsm,
        dsm_valid=core_dsm_valid,
        cell_size_m=cell,
        tdp_radius_m=aircraft.tdp_radius_m,
        cleared_ring_m=aircraft.cleared_ring_m,
        tile_target_cells=mission.tile_target_cells,
    )
    ground = ground_class_raster(
        terrain,
        core_dtm_valid,
        max_slope_deg=aircraft.max_landing_slope_deg,
        max_upslope_deg=aircraft.max_upslope_landing_deg,
        max_roughness_m=aircraft.max_roughness_m,
        max_tdp_obstacle_m=aircraft.tdp_obstacle_height_m,
        max_ring_obstacle_m=aircraft.ring_obstacle_height_m,
        minimum_valid_fraction=mission.minimum_valid_fraction,
        minimum_ring_coverage=mission.minimum_ring_coverage_fraction,
        obstacle_evidence=obstacle_evidence,
    )
    # Exclusion areas (water, wetlands, ...): a touchdown point may not touch them.
    excl_distance_m = None
    if getattr(reader, "has_exclusion", False):
        excl = reader.exclusion_mask(core_r0, core_r1, core_c0, core_c1)
        if excl is not None and excl.any():
            excl_distance_m = distance_to_mask_m(excl, cell)
            passing_ground = (ground == GROUND_PASS) | (ground == GROUND_UPSLOPE_ONLY)
            removed = passing_ground & (excl_distance_m <= aircraft.tdp_radius_m)
            stats["excluded_cells"] = int((removed & candidate_mask).sum())
            ground = np.where(removed, np.uint8(GROUND_FAIL), ground).astype(np.uint8)
    # Listed obstacles: a touchdown point may not contain one taller than the TDP limit, and the cleared ring may not
    # hold one taller than the ring limit. With a DSM the burned surface already does this; without a DSM it is the
    # only protection, so it is applied in both cases.
    stats["cells_removed_by_obstacles"] = 0
    if obstacle_height is not None:
        footprint = obstacle_height[cs]
        passing_ground = (ground == GROUND_PASS) | (ground == GROUND_UPSLOPE_ONLY)
        removed = np.zeros(ground.shape, dtype=bool)
        tall_for_tdp = footprint > aircraft.tdp_obstacle_height_m
        if tall_for_tdp.any():
            removed |= distance_to_mask_m(tall_for_tdp, cell) <= aircraft.tdp_radius_m
        tall_for_ring = footprint > aircraft.ring_obstacle_height_m
        if tall_for_ring.any():
            removed |= distance_to_mask_m(tall_for_ring, cell) <= aircraft.analysis_radius_m
        removed &= passing_ground
        stats["cells_removed_by_obstacles"] = int((removed & candidate_mask).sum())
        ground = np.where(removed, np.uint8(GROUND_FAIL), ground).astype(np.uint8)
    ground_tile = ground[ts].copy()
    ground_tile[~candidate_mask[ts]] = GROUND_NODATA  # outside the AOI -> transparent
    ground_tile[~core_dtm_valid[ts]] = GROUND_NODATA
    stats["ground_pass_cells"] = int(np.isin(ground_tile, (1, 2)).sum())
    if overview_plan is not None:
        # Per-overview-cell sums for this tile; stored with the tile checkpoint so a resumed run needs no rework.
        stats["overview"] = overview_tile_partial(
            overview_plan, tile.row0, tile.row1, tile.col0, tile.col1,
            ground_tile, terrain.slope_deg[ts], terrain.roughness_m[ts], core_dtm[ts],
        )

    ndsm = None
    ndsm_valid = None
    if obstacle_evidence:
        ndsm_valid = core_dtm_valid & core_dsm_valid  # type: ignore[operator]
        ndsm = np.where(ndsm_valid, core_dsm - core_dtm, np.nan).astype(np.float32)  # type: ignore[operator]
        sample = ndsm[::5, ::5][ndsm_valid[::5, ::5]]
        if sample.size >= 1000:
            p05 = float(np.percentile(sample, 5))
            stats["dsm_dtm_p05_m"] = round(p05, 2)
            below = float((sample < -DSM_DTM_TOLERANCE_M).mean())
            stats["dsm_offset_flag"] = int(abs(p05) > DSM_DTM_TOLERANCE_M or below > 0.05)
    tile_mission = replace(mission, max_candidates=mission.max_candidates_per_tile)
    budget_notes: list[str] = []
    candidates, candidate_stats = extract_candidates(
        ground_classes=ground,
        terrain=terrain,
        dtm=core_dtm,
        dtm_valid=core_dtm_valid,
        ndsm=ndsm,
        ndsm_valid=ndsm_valid,
        geometry=core_geometry,
        aircraft=aircraft,
        mission=tile_mission,
        candidate_mask=candidate_mask,
        log=budget_notes.append,
    )
    stats["exact_checks"] = candidate_stats["exact_checks"]
    stats["exact_rejections"] = candidate_stats["exact_rejections"]
    stats["budget_reached"] = bool(budget_notes)

    surface = build_surface(dtm, dtm_valid, dsm, dsm_valid)
    basis = "DSM" if obstacle_evidence else "TERRAIN_ONLY"
    for candidate in candidates:
        local_row, local_col = candidate.row, candidate.col
        candidate.row += core_r0
        candidate.col += core_c0
        candidate.candidate_id = f"T{tile.index}-{candidate.candidate_id}"
        if options.oat_c is not None:
            candidate.density_altitude_ft = round(
                density_altitude_ft(candidate.elevation_m, options.oat_c, options.altimeter_inhg), 0
            )
        sectors = analyze_candidate_approaches(candidate, surface, read_geometry, aircraft, mission, vertical)
        attach_approach_summary(candidate, sectors, mission, basis)
        score_candidate(candidate, aircraft, mission, doctrine, cell, obstacle_evidence, options.performance_mode)
        if vertical is not None:
            found = vertical.nearest(candidate.x, candidate.y, aircraft.approach_range_m)
            if found is not None:
                candidate.nearest_obstacle_m = round(found[0], 1)
                candidate.nearest_obstacle_text = found[1]
        if excl_distance_m is not None:
            nearest = float(excl_distance_m[local_row, local_col])
            if nearest <= 3.0 * aircraft.analysis_radius_m:
                candidate.excluded_distance_m = round(nearest, 1)
            if nearest <= aircraft.analysis_radius_m:
                extra = f"Exclusion area (e.g. water) {nearest:.0f} m from the centre, inside the cleared ring"
                candidate.notes = "; ".join(item for item in (candidate.notes, extra) if item)
        stats["total_sectors"] += len(candidate.sectors)
        stats["incomplete_sectors"] += sum(1 for s in candidate.sectors if s.classification == "INCOMPLETE")
    stats["candidates"] = len(candidates)

    rasters = {"Ground_Class": ground_tile}
    if want_diagnostics:
        rasters["Slope_deg"] = terrain.slope_deg[ts]
        rasters["Roughness_m"] = terrain.roughness_m[ts]
        if obstacle_evidence:
            rasters["Obstacle_Height_m"] = terrain.max_area_obstacle_m[ts]
    return TileOutput(candidates, rasters, stats)


# ---------------------------------------------------------------------------
# Full run
# ---------------------------------------------------------------------------


RASTER_SPECS = {
    "Ground_Class": ("uint8", float(GROUND_NODATA), "Ground test (1 pass, 2 upslope only, 0 fail)"),
    "Slope_deg": ("float32", -9999.0, "Plane-fit slope (deg)"),
    "Roughness_m": ("float32", -9999.0, "Surface roughness (m)"),
    "Obstacle_Height_m": ("float32", -9999.0, "Max obstacle height in LZ + ring (m)"),
}


def run_tiled_pipeline(
    reader: TileReader,
    aircraft: AircraftProfile,
    mission: MissionProfile,
    doctrine: DoctrineProfile,
    options: RunOptions,
    output_folder: str | Path,
    writer: OutputWriter,
    profile_sources: dict[str, str] | None = None,
    progress: Callable[[str], None] | None = None,
    on_tile: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    stop_after_tiles: int | None = None,
) -> RunResult:
    """Run the full screening pipeline over any size of area, tile by tile."""

    started_wall = datetime.now(timezone.utc)
    started = time.perf_counter()
    output_dir = Path(output_folder)
    output_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    def note(message: str, level: int = logging.INFO) -> None:
        LOGGER.log(level, message)
        if level >= logging.WARNING and message not in warnings:
            warnings.append(message)
        if progress is not None:
            progress(message)

    for adapter_note in reader.notes:
        note(adapter_note, logging.WARNING)

    check_profile_compatibility(aircraft, mission)
    if options.max_candidates_override:
        mission = replace(mission, max_candidates=int(options.max_candidates_override))

    search = getattr(reader, "search_area", None)
    geometry = reader.geometry
    cell = geometry.cell_size_m
    tiles, halo, tile_px = plan_for(reader.window, cell, aircraft, mission)
    if not tiles:
        raise AnalysisError("The analysis area does not overlap the DTM")
    buffer_px = buffer_px_for(aircraft, cell)
    row0, row1, col0, col1 = reader.window
    analysis_grid = _sub_geometry(geometry, row0, row1, col0, col1)
    analysed_cells = (row1 - row0) * (col1 - col0)
    area_km2 = analysed_cells * cell * cell / 1.0e6
    if options.diagnostic_rasters.upper() == "ALWAYS":
        want_diagnostics = True
    elif options.diagnostic_rasters.upper() == "NEVER":
        want_diagnostics = False
    else:
        want_diagnostics = analysed_cells <= AUTO_DIAGNOSTIC_LIMIT_CELLS
    overview_cell_m = validate_overview_cell_m(options.overview_cell_m)
    overview_plan = make_overview_plan(reader.window, cell, overview_cell_m) if overview_cell_m else None
    overview_accumulator = OverviewAccumulator(overview_plan) if overview_plan is not None else None
    if overview_plan is not None:
        note(
            f"Terrain overview grid: {overview_plan.block_m:,.0f} m cells, {overview_plan.n_rows * overview_plan.n_cols:,} "
            "cells in the analysis window"
            + (
                f" (the requested {overview_plan.requested_m:,.0f} m was enlarged to {overview_plan.block_m:,.0f} m: "
                "an overview cell holds at least 16 analysis cells)"
                if overview_plan.adjusted
                else ""
            )
        )
    note(
        f"Analysis area {analysis_grid.width:,} x {analysis_grid.height:,} cells at {cell:.3f} m "
        f"({area_km2:,.1f} km2) in {len(tiles)} tile(s) of up to {tile_px:,} x {tile_px:,} cells; "
        f"estimated {estimate_runtime_text(analysed_cells, len(tiles)).split(', ', 1)[1]}"
    )
    if not want_diagnostics:
        note(
            "Large area: slope/roughness/obstacle rasters are skipped (AUTO mode); "
            "Ground_Class and all candidate outputs are still written"
        )
    if cell > 2.5:
        note("Cell size exceeds 2.5 m: results are coarse screening only", logging.WARNING)
    if cell > aircraft.tdp_radius_m / 2.0:
        note(
            f"Cell size {cell:.2f} m is coarse relative to the {aircraft.tdp_diameter_m:.0f} m TDP; "
            "roughness and small obstacles cannot be resolved",
            logging.WARNING,
        )
    obstacle_evidence = reader.has_dsm
    if not obstacle_evidence:
        note(
            "No DSM supplied: trees/buildings are NOT assessed and approaches are checked "
            "against bare terrain only; ratings are capped at Marginal",
            logging.WARNING,
        )
    if search is not None:
        note(f"Search area: {search.description}; landing zones are placed only inside it")
    if getattr(reader, "has_exclusion", False):
        note(f"Exclusion areas applied: {getattr(reader, 'exclusion_description', '')}")
    else:
        note(
            "No exclusion layer: water, wetlands and other unsuitable surfaces are NOT excluded, so a "
            "lake can appear as a perfect landing zone. Add a water polygon layer and check candidates "
            "on imagery",
            logging.WARNING,
        )
    obstacle_set = getattr(reader, "obstacles", None)
    if obstacle_set:
        note(f"Listed obstacles applied: {getattr(reader, 'obstacle_description', obstacle_set.describe())}")
        if obstacle_set.assumed_count:
            note(
                f"{obstacle_set.assumed_count:,} of {len(obstacle_set):,} obstacles have no height in the data: "
                f"the default height is assumed for them. Check the tallest ones.",
                logging.WARNING,
            )
        if not obstacle_evidence:
            note(
                "Listed obstacles are applied to the approaches and the touchdown area, but without a DSM trees "
                "and buildings are still not assessed",
                logging.WARNING,
            )
    else:
        note(
            "No obstacle list: towers, masts and wires are checked only if the DSM captured them, and lidar often "
            "misses thin structures. Add an Obstacles layer or file for a safer result"
        )
    if options.oat_c is not None and aircraft.hover_ceiling_oge_ft is None and aircraft.hover_ceiling_ige_ft is None:
        note(
            "OAT given but the aircraft profile has no hover ceilings: density altitude is "
            "reported but not used to reject sites",
            logging.WARNING,
        )

    # Checkpoint / resume ------------------------------------------------------
    signature = run_signature(
        {
            "version": __version__,
            "dtm": reader.dtm_source,
            "dsm": reader.dsm_source,
            "aoi": reader.aoi_digest,
            "exclusion": getattr(reader, "exclusion_digest", "none"),
            "obstacles": getattr(reader, "obstacle_digest", "none"),
            "window": reader.window,
            "aircraft": asdict(aircraft),
            "mission": asdict(mission),
            "doctrine": asdict(doctrine),
            "options": {k: v for k, v in asdict(options).items() if k != "run_name"},
            "diagnostics": want_diagnostics,
        }
    )
    checkpoint = TileCheckpoint(output_dir, signature, len(tiles))
    done = set(checkpoint.open())
    if done:
        note(f"Resuming: {len(done)} of {len(tiles)} tiles already finished")

    specs = [("Ground_Class", *RASTER_SPECS["Ground_Class"])]
    if want_diagnostics:
        specs += [("Slope_deg", *RASTER_SPECS["Slope_deg"]), ("Roughness_m", *RASTER_SPECS["Roughness_m"])]
        if obstacle_evidence:
            specs.append(("Obstacle_Height_m", *RASTER_SPECS["Obstacle_Height_m"]))
    writer.begin_rasters(analysis_grid, specs, resume=bool(done))

    # Tile loop ------------------------------------------------------------------
    all_candidates: list[Candidate] = []
    totals = {
        "valid_cell_count": 0,
        "ground_pass_cell_count": 0,
        "exact_checks": 0,
        "exact_rejections": 0,
        "tiles_with_budget_reached": 0,
        "tiles_skipped_outside_aoi": 0,
        "incomplete_sectors": 0,
        "total_sectors": 0,
        "cells_removed_by_exclusion": 0,
        "cells_removed_by_obstacles": 0,
    }
    loop_started = time.perf_counter()
    processed_now = 0
    p05_values: list[float] = []
    dsm_tiles_flagged = 0
    for position, tile in enumerate(tiles, start=1):
        if cancel_check is not None and cancel_check():
            raise AnalysisCancelled(
                f"Cancelled after {position - 1} of {len(tiles)} tiles. Run again with the same run "
                "name to resume."
            )
        if stop_after_tiles is not None and processed_now >= stop_after_tiles:
            raise AnalysisCancelled("Stopped early (test hook)")
        if tile.index in done:
            candidates, stats = checkpoint.load_tile(tile)
        else:
            output = analyze_tile(
                tile, reader, aircraft, mission, doctrine, options, halo, buffer_px, want_diagnostics, overview_plan
            )
            tile_geometry = _sub_geometry(geometry, tile.row0, tile.row1, tile.col0, tile.col1)
            for name, array in output.rasters.items():
                writer.write_raster_tile(name, array, tile_geometry, tile)
            candidates, stats = output.candidates, output.stats
            checkpoint.save_tile(tile, candidates, stats)
            processed_now += 1
            for message in stats.get("notes", []):
                note(f"{tile.name}: {message}", logging.WARNING)
        all_candidates.extend(candidates)
        totals["valid_cell_count"] += int(stats.get("valid_cells", 0))
        totals["ground_pass_cell_count"] += int(stats.get("ground_pass_cells", 0))
        totals["exact_checks"] += int(stats.get("exact_checks", 0))
        totals["exact_rejections"] += int(stats.get("exact_rejections", 0))
        totals["tiles_with_budget_reached"] += int(bool(stats.get("budget_reached")))
        totals["tiles_skipped_outside_aoi"] += int(stats.get("skipped") == "outside AOI")
        totals["incomplete_sectors"] += int(stats.get("incomplete_sectors", 0))
        totals["total_sectors"] += int(stats.get("total_sectors", 0))
        totals["cells_removed_by_exclusion"] += int(stats.get("excluded_cells", 0))
        totals["cells_removed_by_obstacles"] += int(stats.get("cells_removed_by_obstacles", 0))
        if overview_accumulator is not None:
            overview_accumulator.add(stats.get("overview"))
        if "dsm_dtm_p05_m" in stats:
            p05_values.append(float(stats["dsm_dtm_p05_m"]))
            dsm_tiles_flagged += int(stats.get("dsm_offset_flag", 0))
        elapsed_loop = time.perf_counter() - loop_started
        eta = ""
        if processed_now and position < len(tiles):
            remaining_s = elapsed_loop / processed_now * (len(tiles) - position)
            if remaining_s < 90:
                eta = f", about {remaining_s:.0f} s left"
            elif remaining_s < 5400:
                eta = f", about {remaining_s / 60:.0f} min left"
            else:
                eta = f", about {remaining_s / 3600:.1f} h left"
        note(
            f"Tile {position}/{len(tiles)}: {stats.get('candidates', 0)} candidate(s)"
            f"{' (outside AOI, skipped)' if stats.get('skipped') else ''}{eta}"
        )
        if on_tile is not None:
            on_tile(position, len(tiles))

    if totals["valid_cell_count"] == 0:
        raise AnalysisError("The DTM contains no valid cells inside the analysis area")
    totals["dsm_minus_dtm_p05_m"] = round(float(np.median(p05_values)), 2) if p05_values else None
    totals["dsm_tiles_flagged"] = dsm_tiles_flagged
    if dsm_tiles_flagged:
        note(
            f"DSM and DTM look inconsistent in {dsm_tiles_flagged} of {len(p05_values)} tile(s): the lowest 5% of "
            f"heights above ground is about {totals['dsm_minus_dtm_p05_m']:+.2f} m, but open ground should be near 0. "
            "Check that both rasters use the same vertical datum and are registered to each other; obstacle "
            "heights and ratings are not reliable until they are",
            logging.WARNING,
        )
    if totals["tiles_with_budget_reached"]:
        note(
            f"{totals['tiles_with_budget_reached']} tile(s) reached the exact-check budget; some "
            "marginal areas in those tiles were not examined",
            logging.WARNING,
        )
    if totals["total_sectors"] and totals["incomplete_sectors"] / totals["total_sectors"] > 0.25:
        note(
            f"{totals['incomplete_sectors'] / totals['total_sectors']:.0%} of approach sectors lack data. "
            f"Provide DTM/DSM coverage extending {aircraft.approach_range_m:,.0f} m beyond the area, or "
            "use a profile with a shorter approach_range_m; affected candidates are rated 'Unassessed'",
            logging.WARNING,
        )

    # Global post-processing -----------------------------------------------------
    if search is not None:
        for candidate in all_candidates:
            distance, bearing = search.nearest(candidate.x, candidate.y)
            candidate.search_distance_m = round(distance, 1)
            candidate.search_bearing_deg = round(bearing, 1)
    separation_m = aircraft.tdp_diameter_m * mission.minimum_candidate_separation_factor
    before = len(all_candidates)
    candidates = suppress_across_tiles(all_candidates, separation_m)
    removed_seam = before - len(candidates)
    if len(candidates) > mission.max_candidates:
        note(
            f"{len(candidates):,} candidates found; keeping the best {mission.max_candidates:,} "
            "(raise 'Maximum number of candidates' to keep more)",
            logging.WARNING,
        )
        candidates = candidates[: mission.max_candidates]
    candidates = rank_candidates(candidates)
    note(
        f"Candidates: {len(candidates):,} after removing {removed_seam:,} tile-seam duplicates "
        f"(exact checks {totals['exact_checks']:,}, rejected {totals['exact_rejections']:,})"
    )

    if candidates:
        converted = None
        try:
            converted = writer.to_latlon([c.x for c in candidates], [c.y for c in candidates])
        except Exception as error:  # noqa: BLE001 - coordinates are a convenience
            note(f"Latitude/longitude conversion failed: {error}", logging.WARNING)
        if converted is not None:
            for candidate, latitude, longitude in zip(candidates, converted[0], converted[1]):
                if latitude is not None and longitude is not None and math.isfinite(latitude):
                    candidate.latitude = round(float(latitude), 7)
                    candidate.longitude = round(float(longitude), 7)
                    candidate.mgrs = latlon_to_mgrs(candidate.latitude, candidate.longitude)

    search_meta = None
    if search is not None:
        centres_latlon: list[list[float]] = []
        try:
            converted_centres = writer.to_latlon([c[0] for c in search.centres], [c[1] for c in search.centres])
            if converted_centres is not None:
                centres_latlon = [[round(float(a), 7), round(float(b), 7)] for a, b in zip(converted_centres[0], converted_centres[1])]
        except Exception as error:  # noqa: BLE001 - only the map circle needs this
            note(f"Search centre latitude/longitude conversion failed: {error}", logging.WARNING)
        search_meta = {"description": search.description, "radius_m": search.radius_m,
                       "centres_xy": [list(c) for c in search.centres], "centres_latlon": centres_latlon}
    obstacle_meta = None
    if obstacle_set:
        markers = obstacle_set.markers()
        converted_markers = None
        try:
            converted_markers = writer.to_latlon([m["x"] for m in markers], [m["y"] for m in markers])
        except Exception as error:  # noqa: BLE001 - only the report map needs this
            note(f"Obstacle latitude/longitude conversion failed: {error}", logging.WARNING)
        marker_rows = []
        if converted_markers is not None:
            for marker, latitude, longitude in zip(markers, converted_markers[0], converted_markers[1]):
                if latitude is not None and longitude is not None and math.isfinite(latitude):
                    marker_rows.append({
                        "lat": round(float(latitude), 6), "lon": round(float(longitude), 6), "agl": round(marker["agl"], 1),
                        "name": marker["name"], "kind": marker["kind"], "assumed": marker["assumed"],
                    })
        obstacle_meta = {
            "description": getattr(reader, "obstacle_description", obstacle_set.describe()),
            "count": len(obstacle_set),
            "assumed_height_count": obstacle_set.assumed_count,
            "buffer_m": obstacle_set.buffer_m,
            "markers_shown": len(marker_rows),
            "markers": marker_rows,
        }
    overview_meta = None
    overview_result = None
    if overview_accumulator is not None:
        overview_result = finalize_overview(overview_accumulator, analysis_grid, aircraft.max_landing_slope_deg)
        attach_candidates(overview_result, candidates)
        overview_meta = overview_result.summary()
        note(
            f"Overview grid: {overview_result.cell_count:,} cells with data; "
            f"{overview_meta['pass_class_counts']['High']:,} High, {overview_meta['pass_class_counts']['Medium']:,} Medium, "
            f"{overview_meta['pass_class_counts']['Low']:,} Low and {overview_meta['pass_class_counts']['None']:,} with no "
            "area passing the ground test"
        )
    note("Finalising rasters...")
    outputs: dict[str, str] = {}
    raster_paths = writer.finish_rasters()
    key_map = {
        "Ground_Class": "ground_class",
        "Slope_deg": "slope",
        "Roughness_m": "roughness",
        "Obstacle_Height_m": "obstacle",
    }
    for name, path in raster_paths.items():
        outputs[key_map.get(name, name)] = path
    note("Writing vector layers...")
    outputs.update(writer.write_vectors(candidates, aircraft, options.report_top_n))
    if search is not None and hasattr(writer, "write_search_area"):
        outputs.update(writer.write_search_area(search))
    if overview_result is not None:
        if hasattr(writer, "write_overview"):
            try:
                outputs.update(writer.write_overview(overview_result))
            except Exception as error:  # noqa: BLE001 - the overview is a convenience; never lose the candidates
                note(f"The overview grid could not be written: {error}", logging.WARNING)
        else:
            note("This output adapter cannot write an overview grid; it was skipped", logging.WARNING)

    elapsed = time.perf_counter() - started
    candidate_extent = None
    if candidates:
        pad = aircraft.analysis_radius_m * 2.0
        xs = [c.x for c in candidates]
        ys = [c.y for c in candidates]
        candidate_extent = {
            "x_min": min(xs) - pad,
            "x_max": max(xs) + pad,
            "y_min": min(ys) - pad,
            "y_max": max(ys) + pad,
        }
    metadata_limit = 1000
    metadata: dict[str, Any] = {
        "run_name": options.run_name,
        "run_started_utc": started_wall.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": round(elapsed, 2),
        "resumed": checkpoint.resumed,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "engine_version": __version__,
        "terrain_method": "fft-moment-plane-fit+rect-union-max (screening) / exact per-candidate check",
        "inputs": {
            "dtm": reader.dtm_source,
            "dsm": reader.dsm_source,
            "crs": reader.crs_description,
            "aoi": reader.aoi_digest,
            "exclusion": getattr(reader, "exclusion_description", "none"),
            "obstacles": getattr(reader, "obstacle_description", "none"),
            "search": search.description if search is not None else None,
        },
        "search": search_meta,
        "obstacles": obstacle_meta,
        "overview": overview_meta,
        "profile_sources": profile_sources or {},
        "profiles": {
            "aircraft": asdict(aircraft),
            "mission": asdict(mission),
            "doctrine": asdict(doctrine),
        },
        "grid": asdict(analysis_grid),
        "dtm_grid": asdict(geometry),
        "cell_size_m": cell,
        "area_km2": round(area_km2, 3),
        "tiling": {
            "tile_count": len(tiles),
            "tile_px": tile_px,
            "halo_px": halo,
            "buffer_px": buffer_px,
            "diagnostic_rasters": want_diagnostics,
        },
        "options": asdict(options),
        "results": {
            **totals,
            "candidate_count": len(candidates),
            "seam_duplicates_removed": removed_seam,
            "rating_counts": rating_counts(candidates),
        },
        "candidate_extent": candidate_extent,
        "outputs": outputs,
        "warnings": warnings,
        "limitations": [
            "Planning and screening aid only; ground or aerial reconnaissance remains required.",
            "Generic profiles contain planning defaults, not approved doctrine or operator data.",
            "Density altitude is a rule-of-thumb approximation, not a performance-chart calculation.",
            (
                "Only the obstacles in the supplied obstacle list, and what the DSM captured, are known. Public obstacle data "
                "is never complete; towers, wires and poles missing from both are not assessed."
                if obstacle_set
                else "Wires, towers and other thin obstacles are usually missing from DSMs, and no obstacle list was supplied."
            ),
            "Threat exposure, surface bearing strength, dust/snow brown-out and live weather are not assessed.",
            "The Ground_Class raster is a fast screening surface; each final candidate was re-checked exactly.",
        ],
        "candidates_note": (
            f"First {min(metadata_limit, len(candidates))} of {len(candidates)} candidates listed; "
            f"approach sectors included for the top {options.report_top_n}. The CSV holds every candidate."
        ),
        "candidates": [
            candidate.to_dict(include_sectors=candidate.rank <= options.report_top_n)
            for candidate in candidates[:metadata_limit]
        ],
    }
    csv_path = write_candidate_csv(output_dir / "HLZ_Candidates.csv", candidates)
    html_path = write_html_report(output_dir / "HLZ_Report.html", candidates, metadata, options.report_top_n)
    outputs["csv"] = str(csv_path)
    outputs["report"] = str(html_path)
    outputs["pilot_brief"] = str(Path(html_path).with_name("HLZ_Pilot_Brief.html"))
    metadata["outputs"] = outputs
    json_path = write_metadata(output_dir / "run_metadata.json", metadata)
    outputs["metadata"] = str(json_path)
    checkpoint.mark_complete()

    summary = console_summary(candidates)
    note(f"Run completed in {elapsed / 60:.1f} min" if elapsed > 120 else f"Run completed in {elapsed:.1f} s")
    return RunResult(candidates=candidates, metadata=metadata, outputs=outputs, summary_lines=summary)


def run_pipeline(
    data: InputData,
    aircraft: AircraftProfile,
    mission: MissionProfile,
    doctrine: DoctrineProfile,
    options: RunOptions,
    output_folder: str | Path,
    writer: OutputWriter,
    profile_sources: dict[str, str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> RunResult:
    """In-memory convenience wrapper (tests, small CLI runs)."""
    return run_tiled_pipeline(
        ArrayTileReader(data),
        aircraft,
        mission,
        doctrine,
        options,
        output_folder,
        writer,
        profile_sources=profile_sources,
        progress=progress,
    )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def configure_logging(
    log_path: str | Path, verbose: bool = False, console: bool = True, append: bool = False
) -> logging.Logger:
    """Configure file (and optionally console) logging for the ``hlz`` logger."""
    from . import COPYRIGHT, LICENSE_ID

    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not append or not path.exists():
        path.write_text(f"# SPDX-License-Identifier: {LICENSE_ID}\n# {COPYRIGHT}\n", encoding="utf-8")
    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        handler.close()
    LOGGER.setLevel(logging.DEBUG)
    LOGGER.propagate = False
    formatter = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)
    if console:
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        stream_handler.setFormatter(formatter)
        LOGGER.addHandler(stream_handler)
    LOGGER.info("HLZ Suitability Tool %s - log %s", __version__, "resumed" if append else "started")
    return LOGGER


def close_logging() -> None:
    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        handler.close()
