# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Unit tests for the v0.6.7 core. Needs only NumPy and SciPy (pytest optional)."""

from __future__ import annotations

import csv
import math
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from hlzcore.approach import analyze_candidate_approaches, attach_approach_summary, build_surface  # noqa: E402
from hlzcore.candidates import extract_candidates  # noqa: E402
from hlzcore.config import (  # noqa: E402
    ProfileError,
    list_profiles,
    load_aircraft_profile,
    load_doctrine_profile,
    load_mission_profile,
    profiles_root,
)
from hlzcore.geo import latlon_to_mgrs  # noqa: E402
from hlzcore.models import (  # noqa: E402
    RATING_HIGHLY_SUITABLE,
    RATING_MARGINAL,
    RATING_UNASSESSED,
    RATING_UNSUITABLE,
    SECTOR_BLOCKED,
    SECTOR_CLEAR,
    SECTOR_INCOMPLETE,
    Candidate,
    GridGeometry,
)
from hlzcore.performance import density_altitude_ft  # noqa: E402
from hlzcore.report import write_candidate_csv  # noqa: E402
from hlzcore.scoring import rank_candidates, score_candidate  # noqa: E402
from hlzcore.terrain import (  # noqa: E402
    GROUND_FAIL,
    GROUND_PASS,
    GROUND_UPSLOPE_ONLY,
    analyze_terrain,
    exact_window_metrics,
    ground_class_raster,
    radii_px,
)

AIRCRAFT = load_aircraft_profile(profiles_root() / "aircraft" / "generic_medium.json")
MISSION = load_mission_profile(profiles_root() / "missions" / "screening.json")
DOCTRINE = load_doctrine_profile(profiles_root() / "doctrine" / "generic_planning.json")
APPROVED = replace(DOCTRINE, approval_status="approved")
SMALL = replace(
    AIRCRAFT, tdp_diameter_m=10.0, cleared_ring_m=4.0, approach_range_m=60.0
)


def _geometry(rows: int, cols: int, cell: float = 1.0) -> GridGeometry:
    return GridGeometry(0.0, rows * cell, cell, -cell, cols, rows)


def _candidate(**overrides) -> Candidate:  # type: ignore[no-untyped-def]
    values = dict(
        candidate_id="C1",
        row=0,
        col=0,
        x=0.0,
        y=0.0,
        elevation_m=100.0,
        slope_deg=1.0,
        aspect_deg=180.0,
        roughness_m=0.05,
        max_tdp_obstacle_m=0.0,
        max_ring_obstacle_m=0.2,
        ring_coverage_fraction=1.0,
        component_area_m2=5000.0,
    )
    values.update(overrides)
    return Candidate(**values)


# --- profiles ---------------------------------------------------------------


def test_all_shipped_profiles_load() -> None:
    assert len(list_profiles("aircraft")) >= 3
    assert len(list_profiles("missions")) >= 2
    assert len(list_profiles("doctrine")) >= 1


def test_profile_rejects_bad_values() -> None:
    folder = Path(tempfile.mkdtemp())
    bad = folder / "bad.json"
    bad.write_text('{"profile_id": "x"}', encoding="utf-8")
    try:
        load_aircraft_profile(bad)
    except ProfileError as error:
        assert "missing" in str(error)
    else:
        raise AssertionError("missing keys must raise ProfileError")


# --- geometry ---------------------------------------------------------------


def test_row_col_uses_floor() -> None:
    geometry = _geometry(10, 10)
    assert geometry.row_col(-0.5, 5.5) == (4, -1)
    assert geometry.row_col(0.5, 10.5) == (-1, 0)
    assert geometry.row_col(3.2, 6.7) == (3, 3)


# --- terrain ----------------------------------------------------------------


def test_plane_fit_slope_and_aspect() -> None:
    rows, cols = np.mgrid[0:120, 0:120]
    dtm = (np.tan(np.radians(4.0)) * cols + 50.0).astype(np.float32)  # rises to the east
    valid = np.ones(dtm.shape, dtype=bool)
    result = analyze_terrain(dtm, valid, None, None, 1.0, 10.0, 5.0, tile_target_cells=20_000)
    assert abs(result.slope_deg[60, 60] - 4.0) < 0.01
    assert abs(result.aspect_deg[60, 60] - 270.0) < 0.5  # downslope faces west
    assert result.roughness_m[60, 60] < 1.0e-3


def test_tiling_matches_single_tile() -> None:
    rng = np.random.default_rng(3)
    dtm = (rng.normal(0, 0.05, (200, 150)) + np.linspace(0, 5, 150)[None, :]).astype(np.float32)
    valid = np.ones(dtm.shape, dtype=bool)
    tiled = analyze_terrain(dtm, valid, None, None, 1.0, 8.0, 4.0, tile_target_cells=10_000)
    single = analyze_terrain(dtm, valid, None, None, 1.0, 8.0, 4.0, tile_target_cells=10_000_000)
    assert np.allclose(tiled.slope_deg, single.slope_deg, equal_nan=True, atol=1.0e-4)
    assert np.allclose(tiled.roughness_m, single.roughness_m, equal_nan=True, atol=1.0e-4)


def test_exact_metrics_detect_bump_and_obstacle() -> None:
    dtm = np.full((80, 80), 10.0, dtype=np.float32)
    dtm[40, 42] += 0.6
    dsm = dtm.copy()
    dsm[40, 50] += 3.0  # inside the ring, outside the TDP
    valid = np.ones(dtm.shape, dtype=bool)
    inner, outer = radii_px(1.0, 5.0, 8.0)
    metrics = exact_window_metrics(40, 40, dtm, valid, dsm - dtm, valid, 1.0, inner, outer)
    assert metrics["max_residual_m"] > 0.5
    assert metrics["tdp_obstacle_m"] == 0.0
    assert abs(metrics["ring_obstacle_m"] - 3.0) < 1.0e-6


def test_zero_cleared_ring_does_not_reject_everything() -> None:
    """v0.3 bug: cleared_ring_m = 0 with a DSM produced zero candidates."""
    dtm = np.full((100, 100), 10.0, dtype=np.float32)
    dsm = dtm.copy()
    valid = np.ones(dtm.shape, dtype=bool)
    aircraft = replace(SMALL, cleared_ring_m=0.0)
    terrain = analyze_terrain(dtm, valid, dsm, valid, 1.0, aircraft.tdp_radius_m, 0.0)
    classes = ground_class_raster(terrain, valid, 7, 15, 0.3, 0.3, 1.0, 0.95, 0.9, True)
    assert (classes == GROUND_PASS).sum() > 1000


def test_ground_classes_upslope_band() -> None:
    rows, cols = np.mgrid[0:150, 0:150]
    dtm = (np.tan(np.radians(10.0)) * cols).astype(np.float32)
    valid = np.ones(dtm.shape, dtype=bool)
    terrain = analyze_terrain(dtm, valid, None, None, 1.0, 5.0, 2.0)
    classes = ground_class_raster(terrain, valid, 7, 15, 0.3, 0.3, 1.0, 0.95, 0.9, False)
    assert classes[75, 75] == GROUND_UPSLOPE_ONLY
    steep = analyze_terrain((dtm * 2).astype(np.float32), valid, None, None, 1.0, 5.0, 2.0)
    steep_classes = ground_class_raster(steep, valid, 7, 15, 0.3, 0.3, 1.0, 0.95, 0.9, False)
    assert steep_classes[75, 75] == GROUND_FAIL


def test_candidates_avoid_ditch_and_respect_separation() -> None:
    dtm = np.full((160, 160), 20.0, dtype=np.float32)
    dtm[78:81, :] -= 1.0  # ditch across the middle
    valid = np.ones(dtm.shape, dtype=bool)
    geometry = _geometry(160, 160)
    terrain = analyze_terrain(dtm, valid, None, None, 1.0, SMALL.tdp_radius_m, SMALL.cleared_ring_m)
    classes = ground_class_raster(terrain, valid, 7, 15, 0.3, 0.3, 1.0, 0.95, 0.9, False)
    candidates, _ = extract_candidates(
        classes, terrain, dtm, valid, None, None, geometry, SMALL, MISSION
    )
    assert candidates
    for candidate in candidates:
        assert abs(candidate.row - 79) > SMALL.tdp_radius_m
    for index, first in enumerate(candidates):
        for second in candidates[index + 1 :]:
            assert math.hypot(first.x - second.x, first.y - second.y) >= SMALL.tdp_diameter_m - 1.0e-6


# --- approach ---------------------------------------------------------------


def test_approach_blocked_by_tower_and_incomplete_at_edge() -> None:
    size = 200
    dtm = np.zeros((size, size), dtype=np.float32)
    dsm = dtm.copy()
    dsm[60:64, 98:102] = 40.0  # tower 38 m north of the candidate
    valid = np.ones(dtm.shape, dtype=bool)
    geometry = _geometry(size, size)
    aircraft = replace(SMALL, approach_range_m=90.0)
    candidate = _candidate(row=100, col=100, x=100.5, y=99.5, elevation_m=0.0)
    surface = build_surface(dtm, valid, dsm, valid)
    sectors = analyze_candidate_approaches(candidate, surface, geometry, aircraft, MISSION)
    by_azimuth = {sector.azimuth_deg: sector for sector in sectors}
    assert by_azimuth[0.0].classification == SECTOR_BLOCKED
    assert by_azimuth[180.0].classification == SECTOR_CLEAR

    edge = _candidate(row=100, col=195, x=195.5, y=99.5, elevation_m=0.0)
    edge_sectors = analyze_candidate_approaches(edge, surface, geometry, aircraft, MISSION)
    east = {sector.azimuth_deg: sector for sector in edge_sectors}[90.0]
    assert east.classification == SECTOR_INCOMPLETE


def test_upslope_candidate_requires_aligned_approach() -> None:
    candidate = _candidate(slope_deg=10.0, aspect_deg=270.0, upslope_only=True)
    from hlzcore.models import ApproachSector

    sectors = [
        ApproachSector(azimuth_deg=az, worst_angle_deg=0.0, classification=SECTOR_CLEAR, coverage_fraction=1.0)
        for az in (0.0, 90.0, 180.0)
    ]
    attach_approach_summary(candidate, sectors, MISSION, "DSM")
    assert candidate.best_approach_azimuth_deg is None  # needs approach from ~270
    sectors.append(
        ApproachSector(azimuth_deg=270.0, worst_angle_deg=1.0, classification=SECTOR_CLEAR, coverage_fraction=1.0)
    )
    attach_approach_summary(candidate, sectors, MISSION, "DSM")
    assert candidate.best_approach_azimuth_deg == 270.0
    assert candidate.landing_heading_deg == 90.0


# --- scoring ----------------------------------------------------------------


def _score(candidate: Candidate, obstacle: bool = True, doctrine=APPROVED) -> Candidate:  # type: ignore[no-untyped-def]
    return score_candidate(candidate, AIRCRAFT, MISSION, doctrine, 0.5, obstacle, "OGE")


def test_rating_differentiates_even_when_not_approved() -> None:
    good = _candidate(best_approach_angle_deg=0.5, clear_sector_count=10, assessed_sector_count=36)
    good.sectors = [None] * 36  # type: ignore[list-item]
    _score(good, doctrine=DOCTRINE)
    assert good.rating == RATING_HIGHLY_SUITABLE
    assert good.status.startswith("Planning only")


def test_no_dsm_caps_rating_at_marginal() -> None:
    candidate = _candidate(best_approach_angle_deg=0.5, clear_sector_count=10, assessed_sector_count=36)
    candidate.sectors = [None] * 36  # type: ignore[list-item]
    _score(candidate, obstacle=False)
    assert candidate.rating == RATING_MARGINAL


def test_blocked_and_unassessed_gates() -> None:
    blocked = _candidate(best_approach_angle_deg=12.0, assessed_sector_count=36)
    blocked.sectors = [None]  # type: ignore[list-item]
    _score(blocked)
    assert blocked.rating == RATING_UNSUITABLE
    unknown = _candidate(best_approach_angle_deg=None, assessed_sector_count=0)
    unknown.sectors = [None]  # type: ignore[list-item]
    _score(unknown)
    assert unknown.rating == RATING_UNASSESSED


def test_ranking_assigns_readable_ids() -> None:
    first = _candidate(rating=RATING_MARGINAL, score=50.0)
    second = _candidate(rating=RATING_HIGHLY_SUITABLE, score=90.0, row=5)
    ranked = rank_candidates([first, second])
    assert ranked[0].candidate_id == "HLZ-01" and ranked[0].score == 90.0
    assert ranked[1].rank == 2


def test_plausibility_mask_drops_sentinels() -> None:
    from hlzcore.pipeline import plausible_elevation_mask

    values = np.array([[100.0, -9999.0], [-3.4028235e38, 8848.0]], dtype=np.float32)
    valid = np.ones(values.shape, dtype=bool)
    notes: list[str] = []
    result = plausible_elevation_mask(values, valid, "DTM", notes)
    assert result.tolist() == [[True, False], [False, True]]
    assert notes and "2 cells" in notes[0]


# --- core window / budgets (v0.6.7) ------------------------------------------


class _NullWriter:
    """Collects raster tiles into full-size arrays; ignores vectors."""

    def begin_rasters(self, grid, specs, resume):  # type: ignore[no-untyped-def]
        self.grid = grid
        self.arrays = {name: np.full((grid.height, grid.width), nodata, dtype=dtype)
                       for name, dtype, nodata, _ in specs}
        self.tiles_written = 0

    def write_raster_tile(self, name, array, tile_geometry, tile):  # type: ignore[no-untyped-def]
        col = int(round((tile_geometry.x_origin - self.grid.x_origin) / self.grid.pixel_width))
        row = int(round((tile_geometry.y_origin - self.grid.y_origin) / self.grid.pixel_height))
        target = self.arrays[name][row : row + array.shape[0], col : col + array.shape[1]]
        assert target.shape == array.shape
        target[...] = array
        self.tiles_written += 1

    def finish_rasters(self):  # type: ignore[no-untyped-def]
        return {name: name for name in self.arrays}

    def write_vectors(self, candidates, aircraft, top_sectors):  # type: ignore[no-untyped-def]
        return {}

    def to_latlon(self, xs, ys):  # type: ignore[no-untyped-def]
        return None


def test_core_window_terrain_matches_full_run() -> None:  # halo correctness
    from hlzcore.terrain import core_halo_px, core_window

    rng = np.random.default_rng(7)
    dtm = (rng.normal(0, 0.1, (300, 300)) + np.linspace(0, 8, 300)[None, :]).astype(np.float32)
    dsm = dtm + (rng.random(dtm.shape) < 0.01) * 5.0
    valid = np.ones(dtm.shape, dtype=bool)
    mask = np.zeros(dtm.shape, dtype=bool)
    mask[120:180, 100:170] = True
    halo = core_halo_px(1.0, SMALL.tdp_radius_m, SMALL.cleared_ring_m)
    r0, r1, c0, c1 = core_window(mask, dtm.shape, halo)
    full = analyze_terrain(dtm, valid, dsm, valid, 1.0, SMALL.tdp_radius_m, SMALL.cleared_ring_m)
    part = analyze_terrain(
        dtm[r0:r1, c0:c1], valid[r0:r1, c0:c1], dsm[r0:r1, c0:c1], valid[r0:r1, c0:c1],
        1.0, SMALL.tdp_radius_m, SMALL.cleared_ring_m,
    )
    inside = mask[r0:r1, c0:c1]
    for name in ("slope_deg", "roughness_m", "max_tdp_obstacle_m", "max_area_obstacle_m", "valid_fraction"):
        a = getattr(full, name)[r0:r1, c0:c1][inside]
        b = getattr(part, name)[inside]
        assert np.allclose(a, b, equal_nan=True, atol=1.0e-4), name


def _scene(size: int = 760, seed: int = 11):  # type: ignore[no-untyped-def]
    """Rolling terrain with a few obstacles: plenty of candidates and failures."""
    from hlzcore.pipeline import InputData

    rows, cols = np.mgrid[0:size, 0:size].astype(np.float64)
    dtm = (40 + 3 * np.sin(rows / 37.0) + 2 * np.cos(cols / 29.0)).astype(np.float32)
    rng = np.random.default_rng(seed)
    dsm = dtm + (rng.random(dtm.shape) < 0.002) * 6.0
    dsm[100:130, 200:260] += 15.0
    valid = np.ones(dtm.shape, dtype=bool)
    return InputData(dtm=dtm, dtm_valid=valid, geometry=_geometry(size, size),
                     dsm=dsm.astype(np.float32), dsm_valid=valid.copy())


TILE_AIRCRAFT = replace(SMALL, approach_range_m=80.0)


def _run(data, max_input_cells: int, folder=None, **kwargs):  # type: ignore[no-untyped-def]
    from hlzcore.pipeline import ArrayTileReader, RunOptions, run_tiled_pipeline

    mission = replace(MISSION, max_input_cells=max_input_cells, max_candidates=5000, max_candidates_per_tile=500)
    writer = _NullWriter()
    result = run_tiled_pipeline(ArrayTileReader(data), TILE_AIRCRAFT, mission, APPROVED, RunOptions(),
                                folder or Path(tempfile.mkdtemp()), writer, **kwargs)
    return result, writer


def test_plan_tiles_cover_window_exactly() -> None:
    from hlzcore.tiling import plan_tiles

    tiles = plan_tiles((5, 1005, 10, 2510), 300)
    coverage = np.zeros((1000, 2500), dtype=np.int32)
    for tile in tiles:
        coverage[tile.row0 - 5 : tile.row1 - 5, tile.col0 - 10 : tile.col1 - 10] += 1
    assert coverage.min() == 1 and coverage.max() == 1
    assert max(t.row1 - t.row0 for t in tiles) <= 300 and max(t.col1 - t.col0 for t in tiles) <= 300


def test_tiled_ground_class_identical_to_single_tile() -> None:
    data = _scene()
    single, single_writer = _run(data, 10_000_000)
    tiled, tiled_writer = _run(data, 250_000)
    assert single.metadata["tiling"]["tile_count"] == 1
    assert tiled.metadata["tiling"]["tile_count"] >= 4
    for name in ("Ground_Class", "Slope_deg", "Roughness_m", "Obstacle_Height_m"):
        a = single_writer.arrays[name]
        b = tiled_writer.arrays[name]
        if a.dtype.kind == "f":
            assert np.allclose(a, b, atol=1.0e-4), name
        else:
            assert np.array_equal(a, b), name


def test_no_duplicates_across_tile_seams() -> None:
    result, _ = _run(_scene(), 250_000)
    separation = TILE_AIRCRAFT.tdp_diameter_m * MISSION.minimum_candidate_separation_factor
    points = np.array([(c.x, c.y) for c in result.candidates])
    assert len(points) > 20
    distances = np.sqrt(((points[:, None, :] - points[None, :, :]) ** 2).sum(-1))
    np.fill_diagonal(distances, np.inf)
    assert distances.min() >= separation - 1.0e-6
    assert [c.rank for c in result.candidates] == list(range(1, len(result.candidates) + 1))


def test_resume_after_interruption_gives_identical_result() -> None:
    from hlzcore.pipeline import AnalysisCancelled
    from hlzcore.tiling import checkpoint_status

    data = _scene()
    reference, _ = _run(data, 250_000)
    folder = Path(tempfile.mkdtemp())
    try:
        _run(data, 250_000, folder=folder, stop_after_tiles=2)
    except AnalysisCancelled:
        pass
    else:
        raise AssertionError("the test hook should stop the run")
    assert checkpoint_status(folder) == "incomplete"
    resumed, writer = _run(data, 250_000, folder=folder)
    assert resumed.metadata["resumed"] is True
    assert checkpoint_status(folder) == "complete"
    key = [(c.candidate_id, c.x, c.y, c.score, c.rating) for c in reference.candidates]
    assert key == [(c.candidate_id, c.x, c.y, c.score, c.rating) for c in resumed.candidates]


def test_resume_refuses_different_inputs() -> None:
    from hlzcore.pipeline import AnalysisCancelled
    from hlzcore.tiling import ResumeMismatch

    folder = Path(tempfile.mkdtemp())
    try:
        _run(_scene(seed=1), 250_000, folder=folder, stop_after_tiles=1)
    except AnalysisCancelled:
        pass
    data = _scene(seed=1)
    data.dtm_source = "a different DTM"
    try:
        _run(data, 250_000, folder=folder)
    except ResumeMismatch:
        return
    raise AssertionError("a checkpoint from other inputs must not be resumed")


def test_tiles_outside_aoi_are_skipped() -> None:
    data = _scene()
    mask = np.zeros(data.dtm.shape, dtype=bool)
    mask[20:120, 20:90] = True
    mask[600:740, 620:740] = True  # two separate AOI parts -> empty tiles between
    data.candidate_mask = mask
    result, writer = _run(data, 250_000)
    assert result.metadata["results"]["tiles_skipped_outside_aoi"] >= 1
    for candidate in result.candidates:
        assert mask[candidate.row, candidate.col]
    ground = writer.arrays["Ground_Class"]
    window = result.metadata["grid"]
    assert ground.shape == (window["height"], window["width"])


def test_large_area_has_no_cell_limit() -> None:
    """60 M-cell area with a 1 M-cell tile budget: no size error, many tiles."""
    from hlzcore.pipeline import plan_for

    tiles, halo, tile_px = plan_for((0, 7746, 0, 7746), 1.0, AIRCRAFT, replace(MISSION, max_input_cells=1_000_000))
    assert len(tiles) > 60 and (tile_px + 2 * halo) ** 2 <= 1_000_000


# --- field guidance (v0.6.7) -------------------------------------------------


def test_hover_guidance_reflects_current_inputs() -> None:
    from hlzcore.help_text import guidance

    assert "NOT USED" in guidance("hover_mode", {"oat_given": False})
    assert "no hover ceilings" in guidance("hover_mode", {"oat_given": True, "has_ceilings": False})
    assert "USED in this run" in guidance("hover_mode", {"oat_given": True, "has_ceilings": True})
    assert "OGE = Out of Ground Effect" in guidance("hover_mode", {})


def test_every_field_help_entry_is_complete() -> None:
    from hlzcore.help_text import FIELD_HELP, tooltip_text

    assert len(FIELD_HELP) >= 19
    for name, entry in FIELD_HELP.items():
        for key in ("title", "what", "requires", "effect"):
            assert entry.get(key), f"{name}: missing {key}"
        assert "Requires:" in tooltip_text(name)


# --- environment check and test orchestration (v0.6.7) -------------------------


def test_path_warning_flags_cloud_sync_and_long_paths() -> None:
    from hlzcore.help_text import path_warning

    assert "cloud-synced" in path_warning(r"C:\Users\xaxis\OneDrive\Documentos\HLZ suitability tool")
    assert "cloud-synced" in path_warning(r"D:\Dropbox\runs")
    assert path_warning(r"C:\GIS\HLZ_runs") is None
    assert path_warning(None) is None
    assert "characters long" in path_warning("C:\\" + "a" * 130)


def _info(env: str = "hlz", arcpy: str | None = "3.7.2", root: str = r"C:\GIS\HLZ", **extra):  # type: ignore[no-untyped-def]
    info = {
        "tool_version": "v0.6.8", "python": "3.13.13", "executable": "x", "env_name": env, "platform": "p",
        "cpu_count": 8, "ram_gb": 32.0, "disk_free_gb": 200.0, "root": root,
        "modules": {"numpy": "2", "scipy": "1", "matplotlib": "3", "arcpy": arcpy, "rasterio": None},
    }
    info.update(extra)
    return info


def test_environment_check_rules() -> None:
    import check_env

    errors, warnings = check_env.problems(_info())
    assert not errors and any("rasterio" in item for item in warnings)  # CLI step is optional
    errors, _ = check_env.problems(_info(env="base"))
    assert any("shared environment" in item for item in errors)
    errors, _ = check_env.problems(_info(env="arcgispro-py3"))
    assert any("shared environment" in item for item in errors)
    errors, _ = check_env.problems(_info(arcpy=None))
    assert any("arcpy is not available" in item for item in errors)
    errors, _ = check_env.problems(_info(arcpy=None), steps=("core", "benchmark"))
    assert not errors  # arcpy is only needed for the smoke test
    _, warnings = check_env.problems(_info(root=r"C:\Users\x\OneDrive\Documentos\HLZ"))
    assert any("cloud-synced" in item for item in warnings)
    _, warnings = check_env.problems(_info(ram_gb=4.0))
    assert any("RAM" in item for item in warnings)


def test_summary_line_extraction() -> None:
    import run_all_tests

    text = "noise\nSUMMARY passed=29 failed=0\n10:00 | done in 3.1 min -> 0.53 million cells/s\nmore noise\n"
    lines = run_all_tests.extract_key_lines(text)
    assert lines == ["SUMMARY passed=29 failed=0", "10:00 | done in 3.1 min -> 0.53 million cells/s"]


# --- exclusion areas, report map, aircraft catalog (v0.6.7) -------------------


def test_rasterize_polygons_even_odd_with_hole() -> None:
    from hlzcore.masks import rasterize_polygons

    grid = GridGeometry(1000.0, 5000.0, 1.0, -1.0, 300, 200)
    outer = [(1100.0, 4900.0), (1200.0, 4900.0), (1200.0, 4800.0), (1100.0, 4800.0)]
    hole = [(1140.0, 4860.0), (1160.0, 4860.0), (1160.0, 4840.0), (1140.0, 4840.0)]
    mask = rasterize_polygons([[outer, hole]], grid)
    assert int(mask.sum()) == 100 * 100 - 20 * 20
    assert not mask[150, 150] and mask[110, 110]  # centre of the hole, and a cell in the ring
    far = rasterize_polygons([[[(9000.0, 9000.0), (9100.0, 9000.0), (9100.0, 8900.0)]]], grid)
    assert not far.any()


def test_rasterize_lines_width_and_edges() -> None:
    from hlzcore.masks import rasterize_lines

    grid = GridGeometry(0.0, 100.0, 1.0, -1.0, 100, 100)
    thin = rasterize_lines([[(10.0, 50.0), (90.0, 50.0)]], grid, 0.0)
    assert int(thin[:, 50].sum()) == 1 and int(thin.sum()) == 81  # one cell wide, end to end
    wide = rasterize_lines([[(10.0, 50.0), (90.0, 50.0)]], grid, 10.0)
    assert int(wide[:, 50].sum()) == 11 and wide[45, 50] and not wide[44, 50]  # 5 m each side of the centre cell
    # A line just outside the grid still counts when it is within half its width of it.
    near = rasterize_lines([[(-4.0, 40.0), (-4.0, 60.0)]], grid, 10.0)
    assert near[50, 0] and not near[50, 2]
    assert not rasterize_lines([[(-60.0, 40.0), (-60.0, 60.0)]], grid, 10.0).any()
    # Several parts, a one-point part and an empty list never fail.
    assert rasterize_lines([[(5.0, 5.0)], [(20.0, 20.0), (30.0, 20.0)]], grid, 2.0).any()
    assert not rasterize_lines([], grid, 10.0).any()
    # A closed ring is NOT filled: only the outline strip is excluded.
    ring = [(30.0, 30.0), (70.0, 30.0), (70.0, 70.0), (30.0, 70.0), (30.0, 30.0)]
    outline = rasterize_lines([ring], grid, 2.0)
    assert outline[30, 40] and not outline[50, 50]


def test_line_exclusion_reader_geojson() -> None:
    """A river LINE in a GeoJSON exclusion file keeps touchdown points off it (needs rasterio)."""
    try:
        import rasterio  # noqa: F401
    except ImportError:
        return
    import json
    import tempfile
    from pathlib import Path as _Path

    from rasterio.transform import from_origin

    from hlzcore.io_rasterio import RasterioTileReader

    folder = _Path(tempfile.mkdtemp())
    path = folder / "dtm.tif"
    with rasterio.open(path, "w", driver="GTiff", width=200, height=200, count=1, dtype="float32",
                       crs="EPSG:32618", transform=from_origin(445000.0, 5030000.0, 1.0, 1.0)) as dataset:
        dataset.write(np.zeros((1, 200, 200), dtype="float32"))
    river = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {
        "type": "LineString", "coordinates": [[445020.0, 5029900.0], [445180.0, 5029900.0]]}}]}
    exclude = folder / "river.geojson"
    exclude.write_text(json.dumps(river), encoding="utf-8")
    reader = RasterioTileReader(str(path), None, "METERS", None, str(exclude), "EPSG:32618", None, None, 20.0)
    try:
        mask = reader.exclusion_mask(0, 200, 0, 200)
        assert reader.has_exclusion and "1 lines widened to 20 m" in reader.exclusion_description
        assert int(mask[:, 100].sum()) == 21 and mask[100, 100] and not mask[80, 100]
        narrow = RasterioTileReader(str(path), None, "METERS", None, str(exclude), "EPSG:32618", None, None, 2.0)
        assert int(narrow.exclusion_mask(0, 200, 0, 200)[:, 100].sum()) == 3
        narrow.close()
    finally:
        reader.close()


def test_distance_to_mask_m() -> None:
    from hlzcore.masks import distance_to_mask_m

    mask = np.zeros((50, 50), dtype=bool)
    mask[25, 25] = True
    distance = distance_to_mask_m(mask, 2.0)
    assert distance[25, 25] == 0.0 and abs(distance[25, 30] - 10.0) < 1.0e-4


def _scene_with_lake():  # type: ignore[no-untyped-def]
    data = _scene()
    lake = np.zeros(data.dtm.shape, dtype=bool)
    lake[300:420, 280:470] = True
    data.exclusion_mask = lake
    return data, lake


def test_exclusion_keeps_touchdown_points_off_water() -> None:
    from scipy import ndimage

    data, lake = _scene_with_lake()
    without = _scene()
    plain, _ = _run(without, 10_000_000)
    with_lake, _ = _run(data, 10_000_000)
    distance = ndimage.distance_transform_edt(~lake)
    on_water_before = [c for c in plain.candidates if distance[c.row, c.col] <= TILE_AIRCRAFT.tdp_radius_m]
    assert on_water_before, "the test scene must put candidates on the lake when it is not excluded"
    for candidate in with_lake.candidates:
        assert distance[candidate.row, candidate.col] > TILE_AIRCRAFT.tdp_radius_m
    assert any(c.excluded_distance_m is not None for c in with_lake.candidates)
    near = [c for c in with_lake.candidates if c.excluded_distance_m is not None and c.excluded_distance_m <= TILE_AIRCRAFT.analysis_radius_m]
    assert all("Exclusion area" in c.notes for c in near)
    assert with_lake.metadata["results"]["cells_removed_by_exclusion"] > 0
    assert any("No exclusion layer" in w for w in plain.metadata["warnings"])
    assert not any("No exclusion layer" in w for w in with_lake.metadata["warnings"])


def test_exclusion_ground_class_identical_when_tiled() -> None:
    data, _ = _scene_with_lake()
    _, single = _run(data, 10_000_000)
    tiled_result, tiled = _run(data, 250_000)
    assert tiled_result.metadata["tiling"]["tile_count"] >= 4
    assert np.array_equal(single.arrays["Ground_Class"], tiled.arrays["Ground_Class"])


class _LatLonWriter(_NullWriter):
    def to_latlon(self, xs, ys):  # type: ignore[no-untyped-def]
        return [45.40 + y * 1.0e-5 for y in ys], [-75.70 + x * 1.4e-5 for x in xs]


def _script_tags_balanced(text: str) -> bool:
    """True when the page's real <script> start and end tags pair up (strings inside scripts are ignored)."""
    from html.parser import HTMLParser

    class Counter(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.starts = 0
            self.ends = 0

        def handle_starttag(self, tag, attrs):  # type: ignore[no-untyped-def]
            self.starts += tag == "script"

        def handle_endtag(self, tag):  # type: ignore[no-untyped-def]
            self.ends += tag == "script"

    counter = Counter()
    counter.feed(text)
    counter.close()
    return counter.starts == counter.ends and counter.starts > 0


def test_report_map_and_clickable_ids() -> None:
    import json
    import re

    from hlzcore.pipeline import ArrayTileReader, RunOptions, run_tiled_pipeline
    from hlzcore.report_map import sanity_check

    assert sanity_check()
    data, _ = _scene_with_lake()
    folder = Path(tempfile.mkdtemp())
    mission = replace(MISSION, max_input_cells=10_000_000, max_candidates=300, max_candidates_per_tile=300)
    for basemap in ("ONLINE", "OFFLINE"):
        result = run_tiled_pipeline(ArrayTileReader(data), TILE_AIRCRAFT, mission, APPROVED,
                                    RunOptions(report_basemap=basemap), folder / basemap, _LatLonWriter())
        html = Path(result.outputs["report"]).read_text(encoding="utf-8")
        payload = json.loads(re.search(r'<script id="hlzdata" type="application/json">(.*?)</script>', html, re.S).group(1).replace("<\\/", "</"))
        assert payload["cfg"]["basemap"] == basemap and len(payload["pts"]) == sum(1 for c in result.candidates if c.site_role == "PRIMARY")  # v0.10.0: one point per site
        first = result.candidates[0]
        assert f"data-id='{first.candidate_id}'" in html and f"id='row-{first.candidate_id}'" in html
        assert "Exclusion areas:" in html and "in-memory mask" in html
        assert len(payload["pts"][0]["sec"]) == 36  # one letter per approach sector
        assert _script_tags_balanced(html)


def test_report_without_coordinates_has_no_map() -> None:
    result, _ = _run(_scene(), 10_000_000)
    html = Path(result.outputs["report"]).read_text(encoding="utf-8")
    assert 'id="hlzdata"' not in html and "Plain overview without a map" in html


def test_aircraft_catalog_profiles() -> None:
    import csv as csv_module

    from hlzcore.config import list_profiles, load_aircraft_profile

    catalog = profiles_root() / "aircraft" / "_catalog" / "rotorcraft_catalog.csv"
    rows = list(csv_module.DictReader(catalog.open(encoding="utf-8-sig", newline="")))
    assert len(rows) >= 25
    labels = list(list_profiles("aircraft"))
    assert len(labels) >= 3 + len(rows)
    assert labels[0].startswith("Generic") and any(label.startswith("Canada:") for label in labels)
    assert labels.index(next(l for l in labels if l.startswith("Canada:"))) < labels.index(next(l for l in labels if l.startswith("United States:")))
    for row in rows:
        profile = load_aircraft_profile(profiles_root() / "aircraft" / row["group"] / f"{row['key']}.json")
        footprint = max(float(row["main_rotor_diameter_m"]), float(row["overall_length_m"]))
        assert profile.tdp_diameter_m >= 1.5 * footprint, row["key"]
        if row["tdp_diameter_m_override"]:
            assert profile.tdp_diameter_m == float(row["tdp_diameter_m_override"]) and "FM 3-21.38" in row["tdp_basis"]
        if row["group"] == "canada":
            assert row["data_status"].startswith("verified") and row["source"].startswith("https://www.canada.ca/")
        assert row["data_status"] and row["source"], row["key"]


def test_exclusion_guidance_text() -> None:
    from hlzcore.help_text import FIELD_HELP, guidance

    assert "NOT excluded" in guidance("in_exclusion", {"exclusion_given": False})
    assert "will not touch" in guidance("in_exclusion", {"exclusion_given": True})
    assert "reveals" in guidance("report_basemap", {"basemap": "ONLINE"})
    assert "no network requests" in guidance("report_basemap", {"basemap": "OFFLINE"})
    assert "in_exclusion" in FIELD_HELP and "report_basemap" in FIELD_HELP


# --- v0.6.7 checks ------------------------------------------------------------------


def test_csv_elevation_is_rounded() -> None:
    result, _ = _run(_scene(), 10_000_000)
    assert result.candidates
    for candidate in result.candidates:
        assert candidate.elevation_m == round(candidate.elevation_m, 2)


def test_dsm_dtm_offset_is_reported() -> None:
    clean, _ = _run(_scene(), 10_000_000)
    assert not any("DSM and DTM look inconsistent" in w for w in clean.metadata["warnings"])
    assert abs(clean.metadata["results"]["dsm_minus_dtm_p05_m"]) < 0.1
    for shift in (2.0, -2.0):
        data = _scene()
        data.dsm = (data.dsm + np.float32(shift)).astype(np.float32)
        result, _ = _run(data, 10_000_000)
        assert any("DSM and DTM look inconsistent" in w for w in result.metadata["warnings"]), shift
        assert abs(result.metadata["results"]["dsm_minus_dtm_p05_m"] - shift) < 0.2


# --- search around a point (v0.6.7) ----------------------------------------------


def test_parse_search_centre_formats() -> None:
    from hlzcore.search import parse_search_centre

    for text in ("45.4236, -75.7009", "45.4236 N 75.7009 W", "N45.4236 W75.7009", "75.7009 W, 45.4236 N"):
        lat, lon, kind = parse_search_centre(text)
        assert kind == "LATLON" and abs(lat - 45.4236) < 1e-9 and abs(lon + 75.7009) < 1e-9, text
    lat, lon, kind = parse_search_centre("45\u00b025'25\"N 75\u00b042'03\"W")
    assert kind == "LATLON" and abs(lat - 45.42361) < 1e-4 and abs(lon + 75.70083) < 1e-4
    lat, lon, kind = parse_search_centre("18T VR 45906 30941")
    assert kind == "MGRS" and abs(lat - 45.4299) < 1e-3 and abs(lon + 75.6915) < 1e-3
    assert parse_search_centre("18TVR4590630941")[:2] == (lat, lon)
    for bad in ("", "hello", "95, 10", "18T ZZ 123", "18T VR 123", "99T VR 1 1"):
        try:
            parse_search_centre(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should be rejected")


def test_mgrs_round_trip_and_independent_library() -> None:
    import random

    from hlzcore.geo import latlon_to_mgrs
    from hlzcore.search import mgrs_to_latlon

    random.seed(9)
    for _ in range(300):
        lat, lon = random.uniform(-79.0, 83.0), random.uniform(-179.0, 179.0)
        back_lat, back_lon = mgrs_to_latlon(latlon_to_mgrs(lat, lon))
        metres = math.hypot((back_lat - lat) * 111_320.0, (back_lon - lon) * 111_320.0 * math.cos(math.radians(lat)))
        assert metres < 1.2, (lat, lon, metres)
    try:
        import mgrs as reference_library
    except ImportError:
        return  # optional independent check
    sw = reference_library.MGRS().toLatLon("18TVR4590630941")
    lat, lon = mgrs_to_latlon("18T VR 45906 30941")
    assert abs(lat - sw[0]) * 111_320.0 < 1.0 and abs(lon - sw[1]) * 78_000.0 < 1.0


def test_search_area_window_mask_and_nearest() -> None:
    from hlzcore.search import SearchArea, intersect_windows

    grid = GridGeometry(1000.0, 5000.0, 2.0, -2.0, 400, 300)
    area = SearchArea([(1300.0, 4700.0), (1500.0, 4500.0)], 120.0)
    mask = area.mask(grid)
    xs = grid.x_origin + (np.arange(grid.width) + 0.5) * grid.pixel_width
    ys = grid.y_origin + (np.arange(grid.height) + 0.5) * grid.pixel_height
    gx, gy = np.meshgrid(xs, ys)
    brute = np.zeros_like(mask)
    for cx, cy in area.centres:
        brute |= (gx - cx) ** 2 + (gy - cy) ** 2 <= 120.0**2
    assert np.array_equal(mask, brute) and mask.any()
    assert area.window(grid) == (90, 300, 90, 310)
    assert SearchArea([(9.0e6, 9.0e6)], 100.0).window(grid) is None
    distance, bearing = area.nearest(1400.0, 4700.0)
    assert abs(distance - 100.0) < 1e-9 and abs(bearing - 90.0) < 1e-9
    assert intersect_windows((0, 10, 0, 10), (5, 20, 8, 30)) == (5, 10, 8, 10) and intersect_windows((0, 5, 0, 5), (6, 9, 6, 9)) is None


def _search_run(data, tile_cells: int):  # type: ignore[no-untyped-def]
    return _run(data, tile_cells)


def test_search_circles_limit_candidates_and_skip_tiles() -> None:
    centres = [(100.0, 660.0), (660.0, 100.0)]  # opposite corners: the two other tiles have nothing to do
    radius = 80.0
    data = _scene()
    data.search_centres, data.search_radius_m = centres, radius
    tiled_result, tiled = _run(data, 250_000)
    assert tiled_result.metadata["tiling"]["tile_count"] >= 4
    assert tiled_result.metadata["results"]["tiles_skipped_outside_aoi"] >= 2
    assert tiled_result.candidates
    for candidate in tiled_result.candidates:
        nearest = min(math.hypot(candidate.x - cx, candidate.y - cy) for cx, cy in centres)
        assert nearest <= radius + 1e-6
        assert candidate.search_distance_m == round(nearest, 1)
        assert candidate.search_bearing_deg is not None and 0.0 <= candidate.search_bearing_deg < 360.0
    assert tiled_result.metadata["search"]["radius_m"] == radius
    assert "radius 80 m around 2 points" in tiled_result.metadata["search"]["description"]
    single_result, single = _run(data, 10_000_000)
    assert np.array_equal(single.arrays["Ground_Class"], tiled.arrays["Ground_Class"])
    assert len(single_result.candidates) > 0


def test_search_bearing_points_the_right_way() -> None:
    data = _scene()
    data.search_centres, data.search_radius_m = [(380.0, 380.0)], 120.0
    result, _ = _run(data, 10_000_000)
    east = [c for c in result.candidates if c.x > 380.0 + 60.0 and abs(c.y - 380.0) < 25.0]
    north = [c for c in result.candidates if c.y > 380.0 + 60.0 and abs(c.x - 380.0) < 25.0]
    assert east and north
    assert all(abs(c.search_bearing_deg - 90.0) < 25.0 for c in east)
    assert all(min(c.search_bearing_deg, 360.0 - c.search_bearing_deg) < 25.0 for c in north)


def test_search_inside_an_aoi_searches_the_overlap() -> None:
    data = _scene()
    box = np.zeros(data.dtm.shape, dtype=bool)
    box[100:500, 100:400] = True
    data.candidate_mask = box
    data.search_centres, data.search_radius_m = [(380.0, 380.0)], 150.0
    result, _ = _run(data, 10_000_000)
    assert result.candidates
    for candidate in result.candidates:
        assert box[candidate.row, candidate.col]
        assert math.hypot(candidate.x - 380.0, candidate.y - 380.0) <= 150.0 + 1e-6


def test_search_circle_outside_the_data_is_an_error() -> None:
    from hlzcore.pipeline import AnalysisError

    data = _scene()
    data.search_centres, data.search_radius_m = [(50_000.0, 50_000.0)], 100.0
    try:
        _run(data, 10_000_000)
    except AnalysisError as error:
        assert "does not overlap" in str(error)
        return
    raise AssertionError("a search circle outside the DTM must be refused")


def test_report_shows_the_search_circle() -> None:
    import json
    import re

    from hlzcore.pipeline import ArrayTileReader, RunOptions, run_tiled_pipeline

    data = _scene()
    data.search_centres, data.search_radius_m = [(380.0, 380.0)], 120.0
    mission = replace(MISSION, max_input_cells=10_000_000, max_candidates=300, max_candidates_per_tile=300)
    result = run_tiled_pipeline(ArrayTileReader(data), TILE_AIRCRAFT, mission, APPROVED, RunOptions(),
                                Path(tempfile.mkdtemp()), _LatLonWriter())
    html = Path(result.outputs["report"]).read_text(encoding="utf-8")
    payload = json.loads(re.search(r'<script id="hlzdata" type="application/json">(.*?)</script>', html, re.S).group(1).replace("<\\/", "</"))
    assert payload["cfg"]["search"]["radius_m"] == 120.0 and len(payload["cfg"]["search"]["centres"]) == 1
    assert payload["pts"][0]["sd"] is not None and "From centre<span class=\"tip" in html and "Search area:" in html
    assert "Dashed blue circle = search radius" in html


def test_search_field_guidance() -> None:
    from hlzcore.help_text import FIELD_HELP, guidance

    for key in ("in_centre", "centre_text", "search_radius_m"):
        assert key in FIELD_HELP
    assert "enter the search radius" in guidance("search_radius_m", {"centre_given": True, "radius_given": False})
    assert "overlap" in guidance("in_centre", {"centre_given": True, "radius_given": True, "aoi_given": True})
    assert "fast even on a very large DTM" in guidance("in_centre", {"centre_given": True, "radius_given": True})


# --- plain-language report and pilot brief (v0.6.7) -----------------------------------


def _sectors(classes):  # type: ignore[no-untyped-def]
    from hlzcore.models import ApproachSector

    return [ApproachSector(azimuth_deg=float(i * 10), worst_angle_deg=0.0, classification=c, coverage_fraction=1.0,
                           worst_x=0.0, worst_y=300.0, worst_distance_m=300.0, worst_height_above_lz_m=12.0) for i, c in enumerate(classes)]


def test_report_text_helpers() -> None:
    from hlzcore.report_text import b3, compass, compass_words, dms

    assert compass(0) == "N" and compass(100) == "E" and compass(112) == "ESE" and compass(359) == "N" and compass(None) == ""
    assert compass_words(250) == "west-south-west" and b3(7.4) == "007" and b3(None) == ""
    assert dms(45.4299, -75.6915) == "45\u00b025\u203248\u2033N 075\u00b041\u203229\u2033W"
    assert dms(-33.5, 151.25).startswith("33\u00b030\u203200\u2033S 151\u00b015")


def test_sector_ranges_wrap_through_north() -> None:
    from hlzcore.models import SECTOR_BLOCKED, SECTOR_CLEAR
    from hlzcore.report_text import sector_ranges

    classes = [SECTOR_CLEAR] * 4 + [SECTOR_BLOCKED] * 9 + [SECTOR_CLEAR] * 23  # clear 130..350 and 0..30; blocked 40..120
    candidate = _candidate()
    candidate.sectors = _sectors(classes)
    assert sector_ranges(candidate, SECTOR_CLEAR, 10.0) == ["125\u00b0\u2013035\u00b0"]
    assert sector_ranges(candidate, SECTOR_BLOCKED, 10.0) == ["035\u00b0\u2013125\u00b0"]
    candidate.sectors = _sectors([SECTOR_CLEAR] * 36)
    assert sector_ranges(candidate, SECTOR_CLEAR, 10.0) == ["all directions"]


def test_plain_summary_wording() -> None:
    from hlzcore.models import SECTOR_CLEAR
    from hlzcore.report_text import plain_summary

    aircraft = {"tdp_diameter_m": 50.0, "cleared_ring_m": 20.0, "max_landing_slope_deg": 7.0, "suitable_angle_deg": 3.0, "max_roughness_m": 0.3}
    good = _candidate(slope_deg=0.3, roughness_m=0.0, max_tdp_obstacle_m=0.0, max_ring_obstacle_m=0.0, best_approach_azimuth_deg=250.0,
                      best_approach_angle_deg=-0.4, landing_heading_deg=70.0, clear_sector_count=29, assessed_sector_count=36, score=99.1, rating="Highly suitable")
    text = plain_summary(good, aircraft)
    assert "almost level" in text and "nothing stands in the 50 m landing circle" in text and "29 of 36" in text
    assert "west-south-west (250\u00b0)" in text and "east-north-east (070\u00b0)" in text and "nothing rises above the horizon" in text
    steep = _candidate(slope_deg=9.7, aspect_deg=269.3, upslope_only=True, max_tdp_obstacle_m=1.4, max_ring_obstacle_m=0.2, best_approach_azimuth_deg=240.0,
                       best_approach_angle_deg=5.0, landing_heading_deg=60.0, rating="Suitable", limiting_factor="Slope 9.7 deg (limit 7 deg)")
    text = plain_summary(steep, aircraft)
    assert "land facing uphill (heading 089\u00b0)" in text and "The tallest object near the pad is 1.4 m" in text and "steep approach is needed" in text and "Weakest point" in text
    no_dsm = _candidate(max_tdp_obstacle_m=None, max_ring_obstacle_m=None)
    assert "NOT checked" in plain_summary(no_dsm, aircraft)
    assert "No approach direction is clear enough" in plain_summary(_candidate(best_approach_azimuth_deg=None), aircraft)
    assert SECTOR_CLEAR  # imported for the helper above


def test_column_help_and_tooltips() -> None:
    from hlzcore.report_text import column_help, howto_html, thead_html

    aircraft = {"tdp_diameter_m": 50.0, "cleared_ring_m": 20.0, "max_landing_slope_deg": 7.0, "max_upslope_landing_deg": 15.0, "max_roughness_m": 0.3,
                "tdp_obstacle_height_m": 0.3, "ring_obstacle_height_m": 1.0, "suitable_angle_deg": 3.0, "unsuitable_angle_deg": 7.0, "clearance_height_m": 2.0, "approach_range_m": 1000.0}
    mission = {"slope_weight": 25.0, "roughness_weight": 20.0, "approach_weight": 30.0, "obstacle_weight": 15.0, "data_quality_weight": 10.0}
    doctrine = {"highly_suitable_score": 80.0}
    plain = column_help(aircraft, mission, doctrine, False)
    with_centre = column_help(aircraft, mission, doctrine, True)
    assert len(with_centre) == len(plain) + 1 and with_centre[5]["head"] == "From centre"
    head = thead_html(with_centre)
    assert head.count("<i>?</i>") == len(with_centre) and "Why it matters:" in head
    for col in with_centre:
        assert col["what"] and col["why"], col["key"]
    approach = next(c for c in plain if c["key"] == "approach")
    assert "from 100" in approach["what"] and "land on 280" in approach["what"].replace("'Land on 280", "land on 280") or "Land on 280" in approach["what"]
    assert "1.2" in approach["what"] and "3\u00b0 or less is clear" in approach["good"]
    assert "30%" in next(c for c in plain if c["key"] == "score")["what"]
    assert "Why it matters" in howto_html(plain, aircraft) and "Bearing" in howto_html(plain, aircraft)


def test_pilot_brief_in_report_and_as_a_file() -> None:
    import json
    import re

    from hlzcore.pipeline import ArrayTileReader, RunOptions, run_tiled_pipeline

    data = _scene()
    mission = replace(MISSION, max_input_cells=10_000_000, max_candidates=300, max_candidates_per_tile=300)
    folder = Path(tempfile.mkdtemp())
    result = run_tiled_pipeline(ArrayTileReader(data), TILE_AIRCRAFT, mission, APPROVED, RunOptions(), folder, _LatLonWriter())
    report = Path(result.outputs["report"]).read_text(encoding="utf-8")
    brief_path = Path(result.outputs["pilot_brief"])
    brief = brief_path.read_text(encoding="utf-8")
    assert brief_path.name == "HLZ_Pilot_Brief.html"
    count = min(30, len(result.candidates))
    assert report.count("class='pcard'") == count and brief.count("class='pcard'") == count
    for text in (report, brief):
        assert "TRUE north" in text and "Wind helper" in text and "Come in from" in text and "In plain words:" in text
        assert re.search(r"\d\d\d&deg; \(", text)
    assert "id='pilot' hidden" in report and 'id="vpilot"' in report and "id='pilot'>" in brief
    assert "id='pdl'" in report and "id='pdl'" not in brief  # the file does not offer to download itself
    payload = json.loads(re.search(r'<script id="pilotdata" type="application/json">(.*?)</script>', brief, re.S).group(1))
    assert len(payload["sites"]) == count and len(payload["sites"][0]["sec"]) == 36
    assert _script_tags_balanced(report) and _script_tags_balanced(brief)


# --- misc -------------------------------------------------------------------


def test_density_altitude_reasonable() -> None:
    value = density_altitude_ft(0.0, 15.0, 29.92)
    assert abs(value) < 1.0
    hot = density_altitude_ft(1000.0, 35.0, 29.92)
    assert 5500.0 < hot < 6500.0


def test_mgrs_known_point() -> None:
    # Parliament Hill, Ottawa (WGS84) -> zone 18T, square VR.
    assert latlon_to_mgrs(45.4236, -75.7009).startswith("18T VR")
    assert latlon_to_mgrs(85.0, 0.0) == ""


def test_csv_header_is_first_row() -> None:
    folder = Path(tempfile.mkdtemp())
    path = write_candidate_csv(folder / "c.csv", [_candidate(rank=1)])
    with path.open(encoding="utf-8-sig") as stream:
        header = next(csv.reader(stream))
    assert header[0] == "Rank" and "MGRS" in header


if __name__ == "__main__":
    failures = 0
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            try:
                function()
                print(f"PASS {name}")
            except Exception as error:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {type(error).__name__}: {error}")
    raise SystemExit(1 if failures else 0)


# --- helicopter dropdown shows ring size (v0.6.8) ----------------------------------------
def test_aircraft_labels_show_sizes_and_guidance() -> None:
    from hlzcore.config import list_profiles, load_aircraft_profile
    from hlzcore.help_text import guidance as field_guidance

    labels = list_profiles("aircraft")
    assert len(labels) >= 29
    for label, path in labels.items():
        profile = load_aircraft_profile(path)
        assert f"{profile.tdp_diameter_m:g} m landing point + {profile.cleared_ring_m:g} m ring" in label
        assert label.endswith(f"[{path.stem}]")  # run-name logic reads the trailing [stem]
    chinook = next(label for label in labels if "[us_ch47f]" in label)
    lakota = next(label for label in labels if "[us_uh72a]" in label)
    assert "80 m landing point + 30 m ring" in chinook and "25 m landing point + 10 m ring" in lakota
    text = field_guidance("aircraft", {"aircraft_sizes": ("CH-47F", 80.0, 30.0)})
    assert text and "70 m in radius" in text
