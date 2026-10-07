# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Terrain overview grid (v0.7.0).

A coarse grid (for example 500 m cells) that summarises the pixel-level results of the analysis, so a large area
- a whole city - can be judged at a glance before the individual candidates are read. For every overview cell it
reports the mean and maximum plane-fit slope, the mean roughness, the elevation range, how much of the cell passes
the ground test and how many ranked candidates it holds.

Why it is built here and not with a fishnet and zonal statistics: every number comes from the same plane-fit slope,
roughness and ground-test rasters that the candidates are chosen from, so the overview can never disagree with the
candidates. The grid is also accumulated tile by tile (and stored in the tile checkpoint), so it costs no extra
memory on a large area and survives an interrupted run.

The module needs only NumPy and works on plain arrays, so it is tested without ArcGIS Pro.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterator

import numpy as np

from .models import RATING_ORDER, Candidate, GridGeometry

GROUND_NODATA = 255  # same value as terrain.GROUND_NODATA (kept here so this module has no heavy imports)
GROUND_PASS = 1
GROUND_UPSLOPE_ONLY = 2

DEFAULT_OVERVIEW_CELL_M = 500.0
MIN_OVERVIEW_CELL_M = 20.0
MAX_OVERVIEW_CELL_M = 20000.0
MIN_BLOCK_CELLS = 16  # smaller blocks would make the per-tile checkpoint records large and the map noisy

# Class limits, written to the run metadata so a reader can see how the classes were made.
PASS_HIGH_PERCENT = 25.0
PASS_MEDIUM_PERCENT = 5.0
TERRAIN_FLAT_FRACTION = 0.5  # mean slope up to this fraction of the aircraft's landing-slope limit is "Flat"

# Sentinels for blocks without a value (JSON has no NaN in the checkpoint files).
SLOPE_NONE = -1.0
ELEV_MIN_NONE = 1.0e9
ELEV_MAX_NONE = -1.0e9

SUM_KEYS = ("n_inside", "n_slope", "sum_slope", "n_rough", "sum_rough", "n_pass", "n_upslope")
ARRAY_KEYS = SUM_KEYS + ("max_slope", "min_elev", "max_elev")

# (field, ArcGIS field type, alias, text length)
OVERVIEW_FIELDS: list[tuple[str, str, str, int]] = [
    ("Cell_ID", "LONG", "Overview cell number", 0),
    ("Cells_Valid", "LONG", "Valid analysed cells", 0),
    ("Pct_Cover", "DOUBLE", "Data coverage of the cell (%)", 0),
    ("Slope_Mean", "DOUBLE", "Mean plane-fit slope (deg)", 0),
    ("Slope_Max", "DOUBLE", "Maximum plane-fit slope (deg)", 0),
    ("Rough_Mean", "DOUBLE", "Mean surface roughness (m)", 0),
    ("Elev_Min", "DOUBLE", "Lowest elevation (m)", 0),
    ("Elev_Max", "DOUBLE", "Highest elevation (m)", 0),
    ("Elev_Range", "DOUBLE", "Elevation range (m)", 0),
    ("Pct_Pass", "DOUBLE", "Area passing the ground test (%)", 0),
    ("Pct_Any", "DOUBLE", "Passing for any landing heading (%)", 0),
    ("Pct_Upslope", "DOUBLE", "Passing for an upslope landing only (%)", 0),
    ("Terr_Class", "TEXT", "Terrain class from mean slope", 12),
    ("Pass_Class", "TEXT", "Share of area passing", 12),
    ("Cand_Count", "LONG", "Ranked candidates inside", 0),
    ("Best_Rating", "TEXT", "Best candidate rating inside", 20),
]


class OverviewError(ValueError):
    """The overview settings are not usable."""


def validate_cell_m(value: float | None) -> float | None:
    """Return a checked overview cell size in metres, or None when the overview is off."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise OverviewError(f"The overview cell size must be a number; received {value!r}") from error
    if number <= 0.0:
        return None
    if not math.isfinite(number) or number < MIN_OVERVIEW_CELL_M or number > MAX_OVERVIEW_CELL_M:
        raise OverviewError(
            f"The overview cell size must be between {MIN_OVERVIEW_CELL_M:g} and {MAX_OVERVIEW_CELL_M:g} m "
            f"(or 0 to switch the overview off); received {number:g}"
        )
    return number


def block_size_cells(cell_size_m: float, overview_cell_m: float) -> int:
    """Edge of one overview cell in analysis cells (never below ``MIN_BLOCK_CELLS``)."""
    if cell_size_m <= 0.0:
        raise OverviewError("The cell size must be positive")
    return max(MIN_BLOCK_CELLS, int(round(overview_cell_m / cell_size_m)))


@dataclass(frozen=True)
class OverviewPlan:
    """Where the overview cells are: aligned to the upper-left corner of the analysis window."""

    window: tuple[int, int, int, int]  # (row0, row1, col0, col1) in full-raster cells
    block: int  # overview cell edge in analysis cells
    cell_size_m: float
    requested_m: float

    @property
    def n_rows(self) -> int:
        return max(0, math.ceil((self.window[1] - self.window[0]) / self.block))

    @property
    def n_cols(self) -> int:
        return max(0, math.ceil((self.window[3] - self.window[2]) / self.block))

    @property
    def block_m(self) -> float:
        return self.block * self.cell_size_m

    @property
    def adjusted(self) -> bool:
        """True when the cell had to be enlarged because the requested size was under ``MIN_BLOCK_CELLS`` cells."""
        return abs(self.block_m - self.requested_m) > 0.5 * self.cell_size_m + 1.0e-9


def make_plan(window: tuple[int, int, int, int], cell_size_m: float, overview_cell_m: float) -> OverviewPlan:
    return OverviewPlan(
        window=tuple(int(v) for v in window),  # type: ignore[arg-type]
        block=block_size_cells(cell_size_m, overview_cell_m),
        cell_size_m=float(cell_size_m),
        requested_m=float(overview_cell_m),
    )


# ---------------------------------------------------------------------------
# Block reduction
# ---------------------------------------------------------------------------


def _layout(plan: OverviewPlan, row0: int, row1: int, col0: int, col1: int) -> tuple[int, int, int, int, int, int]:
    block = plan.block
    rel_r = row0 - plan.window[0]
    rel_c = col0 - plan.window[2]
    if rel_r < 0 or rel_c < 0:
        raise OverviewError("A tile starts before the analysis window")
    off_r = rel_r % block
    off_c = rel_c % block
    n_br = math.ceil((off_r + (row1 - row0)) / block)
    n_bc = math.ceil((off_c + (col1 - col0)) / block)
    return rel_r // block, rel_c // block, off_r, off_c, n_br, n_bc


def _blocked(array: np.ndarray, layout: tuple[int, int, int, int, int, int], block: int, fill: Any) -> np.ndarray:
    """Pad ``array`` so it starts on a block boundary and reshape to (block rows, block, block cols, block)."""
    _, _, off_r, off_c, n_br, n_bc = layout
    padded = np.full((n_br * block, n_bc * block), fill, dtype=array.dtype)
    padded[off_r : off_r + array.shape[0], off_c : off_c + array.shape[1]] = array
    return padded.reshape(n_br, block, n_bc, block)


def tile_partial(
    plan: OverviewPlan,
    row0: int,
    row1: int,
    col0: int,
    col1: int,
    ground: np.ndarray,
    slope_deg: np.ndarray,
    roughness_m: np.ndarray,
    elevation_m: np.ndarray,
) -> dict[str, Any] | None:
    """Per-overview-cell sums for one tile, as plain lists (stored in the tile checkpoint).

    ``ground`` is the tile's ground-class raster (255 = outside the AOI or no data); the other arrays hold the
    same tile. Returns None when the tile holds no valid cell.
    """
    shape = (row1 - row0, col1 - col0)
    for name, array in (("ground", ground), ("slope", slope_deg), ("roughness", roughness_m), ("elevation", elevation_m)):
        if array.shape != shape:
            raise OverviewError(f"The {name} array is {array.shape}, but the tile is {shape}")
    inside = ground != GROUND_NODATA
    if not inside.any():
        return None
    layout = _layout(plan, row0, row1, col0, col1)
    block = plan.block
    with np.errstate(invalid="ignore"):
        slope_ok = inside & np.isfinite(slope_deg)
        rough_ok = inside & np.isfinite(roughness_m)
        elev_ok = inside & np.isfinite(elevation_m)

    def count(mask: np.ndarray) -> np.ndarray:
        return _blocked(mask, layout, block, False).sum(axis=(1, 3))

    def total(values: np.ndarray, ok: np.ndarray) -> np.ndarray:
        data = np.where(ok, values, 0.0).astype(np.float64)
        return _blocked(data, layout, block, 0.0).sum(axis=(1, 3))

    slope_max = _blocked(np.where(slope_ok, slope_deg, SLOPE_NONE).astype(np.float64), layout, block, SLOPE_NONE).max(axis=(1, 3))
    elev_min = _blocked(np.where(elev_ok, elevation_m, ELEV_MIN_NONE).astype(np.float64), layout, block, ELEV_MIN_NONE).min(axis=(1, 3))
    elev_max = _blocked(np.where(elev_ok, elevation_m, ELEV_MAX_NONE).astype(np.float64), layout, block, ELEV_MAX_NONE).max(axis=(1, 3))
    arrays = {
        "n_inside": count(inside),
        "n_slope": count(slope_ok),
        "sum_slope": total(slope_deg, slope_ok),
        "n_rough": count(rough_ok),
        "sum_rough": total(roughness_m, rough_ok),
        "n_pass": count(ground == GROUND_PASS),
        "n_upslope": count(ground == GROUND_UPSLOPE_ONLY),
        "max_slope": slope_max,
        "min_elev": elev_min,
        "max_elev": elev_max,
    }
    block_row0, block_col0, _, _, n_br, n_bc = layout
    partial: dict[str, Any] = {"br0": int(block_row0), "bc0": int(block_col0), "shape": [int(n_br), int(n_bc)]}
    for key in ARRAY_KEYS:
        values = arrays[key]
        partial[key] = np.round(values, 4).tolist() if values.dtype.kind == "f" else values.astype(np.int64).tolist()
    return partial


class OverviewAccumulator:
    """Adds up the per-tile partials into the overview of the whole analysis window."""

    def __init__(self, plan: OverviewPlan):
        self.plan = plan
        shape = (plan.n_rows, plan.n_cols)
        self.arrays: dict[str, np.ndarray] = {key: np.zeros(shape, dtype=np.float64) for key in SUM_KEYS}
        self.arrays["max_slope"] = np.full(shape, SLOPE_NONE, dtype=np.float64)
        self.arrays["min_elev"] = np.full(shape, ELEV_MIN_NONE, dtype=np.float64)
        self.arrays["max_elev"] = np.full(shape, ELEV_MAX_NONE, dtype=np.float64)
        self.tiles_added = 0

    def add(self, partial: dict[str, Any] | None) -> None:
        if not partial:
            return
        br0, bc0 = int(partial["br0"]), int(partial["bc0"])
        n_br, n_bc = (int(v) for v in partial["shape"])
        if br0 < 0 or bc0 < 0 or br0 + n_br > self.plan.n_rows or bc0 + n_bc > self.plan.n_cols:
            raise OverviewError("An overview partial lies outside the overview grid (stale checkpoint?)")
        target = (slice(br0, br0 + n_br), slice(bc0, bc0 + n_bc))
        for key in SUM_KEYS:
            self.arrays[key][target] += np.asarray(partial[key], dtype=np.float64).reshape(n_br, n_bc)
        np.maximum(self.arrays["max_slope"][target], np.asarray(partial["max_slope"], dtype=np.float64).reshape(n_br, n_bc),
                   out=self.arrays["max_slope"][target])
        np.minimum(self.arrays["min_elev"][target], np.asarray(partial["min_elev"], dtype=np.float64).reshape(n_br, n_bc),
                   out=self.arrays["min_elev"][target])
        np.maximum(self.arrays["max_elev"][target], np.asarray(partial["max_elev"], dtype=np.float64).reshape(n_br, n_bc),
                   out=self.arrays["max_elev"][target])
        self.tiles_added += 1


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


def terrain_class(mean_slope_deg: float | None, landing_slope_limit_deg: float) -> str:
    if mean_slope_deg is None or not math.isfinite(mean_slope_deg):
        return "Unknown"
    if mean_slope_deg <= TERRAIN_FLAT_FRACTION * landing_slope_limit_deg:
        return "Flat"
    if mean_slope_deg <= landing_slope_limit_deg:
        return "Moderate"
    return "Steep"


def pass_class(percent_pass: float) -> str:
    if percent_pass >= PASS_HIGH_PERCENT:
        return "High"
    if percent_pass >= PASS_MEDIUM_PERCENT:
        return "Medium"
    if percent_pass > 0.0:
        return "Low"
    return "None"


@dataclass
class OverviewResult:
    """The finished overview, ready to be written as polygons by a GIS adapter."""

    plan: OverviewPlan
    grid: GridGeometry  # the analysis window
    numbers: dict[str, np.ndarray]  # float arrays; NaN = no value
    terrain: np.ndarray  # str
    passing: np.ndarray  # str
    best_rating: np.ndarray  # str
    candidate_count: np.ndarray  # int
    valid: np.ndarray  # bool: the cell holds at least one valid analysed cell
    landing_slope_limit_deg: float

    @property
    def cell_count(self) -> int:
        return int(self.valid.sum())

    def summary(self) -> dict[str, Any]:
        classes = {name: int((self.passing[self.valid] == name).sum()) for name in ("High", "Medium", "Low", "None")}
        return {
            "overview_cell_m": round(self.plan.block_m, 3),
            "requested_cell_m": self.plan.requested_m,
            "cell_enlarged": self.plan.adjusted,
            "cells": self.cell_count,
            "pass_class_counts": classes,
            "cells_with_candidates": int(((self.candidate_count > 0) & self.valid).sum()),
            "limits": {
                "pass_high_percent": PASS_HIGH_PERCENT,
                "pass_medium_percent": PASS_MEDIUM_PERCENT,
                "flat_if_mean_slope_up_to_deg": round(TERRAIN_FLAT_FRACTION * self.landing_slope_limit_deg, 3),
                "moderate_if_mean_slope_up_to_deg": self.landing_slope_limit_deg,
            },
        }

    def rows(self) -> Iterator[tuple[int, list[tuple[float, float]], dict[str, Any]]]:
        """(cell number, rectangle ring, attributes) for every valid overview cell, row by row."""
        grid = self.grid
        block = self.plan.block
        x_end = grid.x_origin + grid.width * grid.pixel_width
        y_end = grid.y_origin + grid.height * grid.pixel_height
        number = 0
        n = self.numbers
        for br in range(self.plan.n_rows):
            for bc in range(self.plan.n_cols):
                number += 1
                if not self.valid[br, bc]:
                    continue
                x0 = grid.x_origin + bc * block * grid.pixel_width
                x1 = grid.x_origin + (bc + 1) * block * grid.pixel_width
                y0 = grid.y_origin + br * block * grid.pixel_height
                y1 = grid.y_origin + (br + 1) * block * grid.pixel_height
                if grid.pixel_width > 0:
                    x1 = min(x1, x_end)
                else:
                    x1 = max(x1, x_end)
                if grid.pixel_height < 0:
                    y1 = max(y1, y_end)
                else:
                    y1 = min(y1, y_end)
                ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]

                def value(key: str) -> float | None:
                    item = float(n[key][br, bc])
                    return round(item, 3) if math.isfinite(item) else None

                attributes = {
                    "Cell_ID": number,
                    "Cells_Valid": int(n["cells_valid"][br, bc]),
                    "Pct_Cover": value("pct_cover"),
                    "Slope_Mean": value("slope_mean"),
                    "Slope_Max": value("slope_max"),
                    "Rough_Mean": value("rough_mean"),
                    "Elev_Min": value("elev_min"),
                    "Elev_Max": value("elev_max"),
                    "Elev_Range": value("elev_range"),
                    "Pct_Pass": value("pct_pass"),
                    "Pct_Any": value("pct_any"),
                    "Pct_Upslope": value("pct_upslope"),
                    "Terr_Class": str(self.terrain[br, bc]),
                    "Pass_Class": str(self.passing[br, bc]),
                    "Cand_Count": int(self.candidate_count[br, bc]),
                    "Best_Rating": str(self.best_rating[br, bc]) or None,
                }
                yield number, ring, attributes


def finalize(
    accumulator: OverviewAccumulator,
    grid: GridGeometry,
    landing_slope_limit_deg: float,
) -> OverviewResult:
    """Turn the accumulated sums into the overview attributes."""
    plan = accumulator.plan
    a = accumulator.arrays
    n_inside = a["n_inside"]
    valid = n_inside > 0
    block = plan.block
    row0, row1, col0, col1 = plan.window
    row_cells = np.array([min(row1, row0 + (i + 1) * block) - (row0 + i * block) for i in range(plan.n_rows)], dtype=np.float64)
    col_cells = np.array([min(col1, col0 + (i + 1) * block) - (col0 + i * block) for i in range(plan.n_cols)], dtype=np.float64)
    cells_total = np.outer(row_cells, col_cells)

    def ratio(numerator: np.ndarray, denominator: np.ndarray, scale: float = 1.0) -> np.ndarray:
        out = np.full(numerator.shape, np.nan)
        good = denominator > 0
        out[good] = scale * numerator[good] / denominator[good]
        return out

    slope_mean = ratio(a["sum_slope"], a["n_slope"])
    slope_max = np.where(a["max_slope"] > SLOPE_NONE, a["max_slope"], np.nan)
    elev_min = np.where(a["min_elev"] < ELEV_MIN_NONE, a["min_elev"], np.nan)
    elev_max = np.where(a["max_elev"] > ELEV_MAX_NONE, a["max_elev"], np.nan)
    pct_any = ratio(a["n_pass"], n_inside, 100.0)
    pct_upslope = ratio(a["n_upslope"], n_inside, 100.0)
    pct_pass = np.where(valid, np.nan_to_num(pct_any) + np.nan_to_num(pct_upslope), np.nan)
    numbers = {
        "cells_valid": n_inside,
        "pct_cover": ratio(n_inside, cells_total, 100.0),
        "slope_mean": slope_mean,
        "slope_max": slope_max,
        "rough_mean": ratio(a["sum_rough"], a["n_rough"]),
        "elev_min": elev_min,
        "elev_max": elev_max,
        "elev_range": elev_max - elev_min,
        "pct_pass": pct_pass,
        "pct_any": pct_any,
        "pct_upslope": pct_upslope,
    }
    terrain = np.full(valid.shape, "", dtype=object)
    passing = np.full(valid.shape, "", dtype=object)
    for br, bc in zip(*np.nonzero(valid)):
        terrain[br, bc] = terrain_class(float(slope_mean[br, bc]), landing_slope_limit_deg)
        passing[br, bc] = pass_class(float(pct_pass[br, bc]))
    return OverviewResult(
        plan=plan,
        grid=grid,
        numbers=numbers,
        terrain=terrain,
        passing=passing,
        best_rating=np.full(valid.shape, "", dtype=object),
        candidate_count=np.zeros(valid.shape, dtype=np.int64),
        valid=valid,
        landing_slope_limit_deg=float(landing_slope_limit_deg),
    )


def attach_candidates(result: OverviewResult, candidates: list[Candidate]) -> None:
    """Count the ranked candidates in every overview cell and note the best rating there."""
    plan = result.plan
    row0, _, col0, _ = plan.window
    best_index = np.full(result.valid.shape, len(RATING_ORDER), dtype=np.int64)
    for candidate in candidates:
        br = (candidate.row - row0) // plan.block
        bc = (candidate.col - col0) // plan.block
        if 0 <= br < plan.n_rows and 0 <= bc < plan.n_cols:
            result.candidate_count[br, bc] += 1
            try:
                best_index[br, bc] = min(best_index[br, bc], RATING_ORDER.index(candidate.rating))
            except ValueError:
                continue
    for br, bc in zip(*np.nonzero(result.candidate_count > 0)):
        index = best_index[br, bc]
        result.best_rating[br, bc] = RATING_ORDER[index] if index < len(RATING_ORDER) else ""
