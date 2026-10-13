# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Unit tests for the v0.7.0 terrain overview grid and point exclusions. Needs only NumPy and SciPy."""

from __future__ import annotations

import json
import math
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from hlzcore.models import GridGeometry  # noqa: E402
from hlzcore.overview import (  # noqa: E402
    MIN_BLOCK_CELLS,
    OverviewAccumulator,
    OverviewError,
    finalize,
    make_plan,
    pass_class,
    terrain_class,
    tile_partial,
    validate_cell_m,
)
from test_core import (  # noqa: E402
    APPROVED,
    MISSION,
    TILE_AIRCRAFT,
    _NullWriter,
    _scene,
)


# --- point exclusions --------------------------------------------------------------------------


def test_rasterize_points_radius_and_edges() -> None:
    from hlzcore.masks import rasterize_points

    grid = GridGeometry(1000.0, 5000.0, 2.0, -2.0, 60, 50)  # 2 m cells
    centre = (1060.0, 4950.0)  # the middle of the grid
    mask = rasterize_points([centre], grid, 10.0)
    # Brute force: every cell centre within 10 m of the point.
    rows, cols = np.mgrid[0:50, 0:60]
    cx = 1000.0 + (cols + 0.5) * 2.0
    cy = 5000.0 - (rows + 0.5) * 2.0
    expected = (cx - centre[0]) ** 2 + (cy - centre[1]) ** 2 <= 100.0
    expected[int(math.floor((5000.0 - centre[1]) / 2.0)), int(math.floor((centre[0] - 1000.0) / 2.0))] = True
    assert np.array_equal(mask, expected)
    # Radius 0 marks only the cell that holds the point.
    single = rasterize_points([centre], grid, 0.0)
    assert int(single.sum()) == 1 and single[25, 30]
    # A point just outside the grid still counts when it is within the radius of it; one far away does not.
    near = rasterize_points([(995.0, 4950.0)], grid, 10.0)
    assert near[25, 0] and not near[25, 6]
    assert not rasterize_points([(900.0, 4950.0)], grid, 10.0).any()
    # Empty input, NaN and several points never fail.
    assert not rasterize_points([], grid, 10.0).any()
    assert not rasterize_points([(float("nan"), 1.0)], grid, 10.0).any()
    two = rasterize_points([(1010.0, 4990.0), (1100.0, 4910.0)], grid, 4.0)
    assert two[5, 5] and two[45, 50] and int(two.sum()) > 2


def test_point_exclusion_keeps_touchdown_points_away() -> None:
    from scipy import ndimage

    from hlzcore.masks import rasterize_points
    from test_core import _run

    data = _scene()
    plain, _ = _run(_scene(), 10_000_000)
    hazards = [(candidate.x, candidate.y) for candidate in plain.candidates[:6]]
    assert hazards, "the scene must produce candidates"
    radius = 12.0
    data.exclusion_mask = rasterize_points(hazards, data.geometry, radius)
    result, _ = _run(data, 10_000_000)
    for x, y in hazards:
        for candidate in result.candidates:
            distance = math.hypot(candidate.x - x, candidate.y - y)
            assert distance > TILE_AIRCRAFT.tdp_radius_m + radius - 1.0, "a candidate sits inside a hazard circle"
    assert result.metadata["results"]["cells_removed_by_exclusion"] > 0
    distance = ndimage.distance_transform_edt(~data.exclusion_mask)
    assert all(distance[c.row, c.col] > TILE_AIRCRAFT.tdp_radius_m for c in result.candidates)


def test_point_exclusion_reader_geojson() -> None:
    """Point and MultiPoint features in a GeoJSON exclusion file are read and widened (needs rasterio)."""
    try:
        import rasterio  # noqa: F401
    except ImportError:
        return
    from rasterio.transform import from_origin

    from hlzcore.io_rasterio import RasterioTileReader

    folder = Path(tempfile.mkdtemp())
    path = folder / "dtm.tif"
    with rasterio.open(path, "w", driver="GTiff", width=200, height=200, count=1, dtype="float32",
                       crs="EPSG:32618", transform=from_origin(445000.0, 5030000.0, 1.0, 1.0)) as dataset:
        dataset.write(np.zeros((1, 200, 200), dtype="float32"))
    document = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [445050.0, 5029950.0]}},
        {"type": "Feature", "properties": {}, "geometry": {"type": "MultiPoint", "coordinates": [[445150.0, 5029850.0], [445170.0, 5029830.0]]}},
    ]}
    exclude = folder / "hazards.geojson"
    exclude.write_text(json.dumps(document), encoding="utf-8")
    reader = RasterioTileReader(str(path), None, "METERS", None, str(exclude), "EPSG:32618", None, None, 10.0, 6.0)
    try:
        mask = reader.exclusion_mask(0, 200, 0, 200)
        assert reader.has_exclusion and "3 points with a 6 m radius" in reader.exclusion_description
        assert mask[50, 50] and mask[50, 55] and not mask[50, 58]
        assert mask[150, 150] and mask[170, 170] and not mask[100, 100]
        # A window that starts just right of the point at x=50 still sees the part of its 6 m circle that reaches in.
        edge = reader.exclusion_mask(0, 200, 53, 200)
        assert edge[50, 0] and edge[50, 2] and not edge[50, 5]
        # A window far from every point sees nothing.
        assert not reader.exclusion_mask(0, 40, 90, 140).any()
        other = RasterioTileReader(str(path), None, "METERS", None, str(exclude), "EPSG:32618", None, None, 10.0, 20.0)
        assert other.exclusion_digest != reader.exclusion_digest  # the radius is part of the resume signature
        other.close()
    finally:
        reader.close()


# --- overview grid: settings -----------------------------------------------------------------


def test_overview_cell_validation_and_plan() -> None:
    assert validate_cell_m(None) is None and validate_cell_m(0) is None and validate_cell_m(-5) is None
    assert validate_cell_m(500) == 500.0
    for bad in (10, 50000, "abc", float("nan")):
        try:
            validate_cell_m(bad)  # type: ignore[arg-type]
        except OverviewError:
            continue
        raise AssertionError(f"{bad!r} should be refused")
    plan = make_plan((0, 1000, 0, 1000), 1.0, 500.0)
    assert plan.block == 500 and plan.n_rows == 2 and plan.n_cols == 2 and not plan.adjusted
    coarse = make_plan((0, 1000, 0, 1000), 10.0, 50.0)  # 5 cells requested, 16 is the minimum
    assert coarse.block == MIN_BLOCK_CELLS and coarse.adjusted and abs(coarse.block_m - 160.0) < 1.0e-9
    odd = make_plan((5, 305, 7, 207), 1.0, 128.0)
    assert odd.n_rows == 3 and odd.n_cols == 2


def test_overview_classes() -> None:
    assert terrain_class(1.0, 7.0) == "Flat" and terrain_class(3.5, 7.0) == "Flat"
    assert terrain_class(5.0, 7.0) == "Moderate" and terrain_class(9.0, 7.0) == "Steep"
    assert terrain_class(None, 7.0) == "Unknown" and terrain_class(float("nan"), 7.0) == "Unknown"
    assert pass_class(60.0) == "High" and pass_class(25.0) == "High" and pass_class(10.0) == "Medium"
    assert pass_class(1.0) == "Low" and pass_class(0.0) == "None"


# --- overview grid: arithmetic -----------------------------------------------------------------


def _random_arrays(rows: int, cols: int, seed: int):  # type: ignore[no-untyped-def]
    rng = np.random.default_rng(seed)
    ground = rng.choice([0, 1, 2, 255], size=(rows, cols), p=[0.3, 0.35, 0.15, 0.2]).astype(np.uint8)
    slope = rng.uniform(0, 12, (rows, cols)).astype(np.float32)
    slope[rng.random((rows, cols)) < 0.05] = np.nan
    rough = rng.uniform(0, 0.4, (rows, cols)).astype(np.float32)
    rough[rng.random((rows, cols)) < 0.05] = np.nan
    elevation = (100 + rng.normal(0, 5, (rows, cols))).astype(np.float32)
    return ground, slope, rough, elevation


def test_overview_blocks_match_brute_force_across_unaligned_tiles() -> None:
    rows, cols = 173, 151
    window = (10, 10 + rows, 20, 20 + cols)
    plan = make_plan(window, 1.0, 40.0)  # 40-cell blocks; the window edge cuts the last blocks
    assert plan.block == 40
    ground, slope, rough, elevation = _random_arrays(rows, cols, 5)
    accumulator = OverviewAccumulator(plan)
    # Tiles whose edges do NOT fall on block boundaries.
    row_edges = [0, 37, 101, rows]
    col_edges = [0, 59, 120, cols]
    for r in range(3):
        for c in range(3):
            r0, r1, c0, c1 = row_edges[r], row_edges[r + 1], col_edges[c], col_edges[c + 1]
            partial = tile_partial(
                plan, window[0] + r0, window[0] + r1, window[2] + c0, window[2] + c1,
                ground[r0:r1, c0:c1], slope[r0:r1, c0:c1], rough[r0:r1, c0:c1], elevation[r0:r1, c0:c1],
            )
            # The partial must survive a JSON round trip (it is stored in the tile checkpoint).
            accumulator.add(json.loads(json.dumps(partial)))
    result = finalize(accumulator, GridGeometry(0.0, 1000.0, 1.0, -1.0, cols, rows), 7.0)
    assert (result.plan.n_rows, result.plan.n_cols) == (5, 4)
    for br in range(plan.n_rows):
        for bc in range(plan.n_cols):
            sl = (slice(br * 40, min(rows, (br + 1) * 40)), slice(bc * 40, min(cols, (bc + 1) * 40)))
            g, s, q, e = ground[sl], slope[sl], rough[sl], elevation[sl]
            inside = g != 255
            assert result.numbers["cells_valid"][br, bc] == inside.sum()
            if not inside.any():
                assert not result.valid[br, bc]
                continue
            ok = inside & np.isfinite(s)
            assert abs(result.numbers["slope_mean"][br, bc] - s[ok].astype(np.float64).mean()) < 1.0e-3
            assert abs(result.numbers["slope_max"][br, bc] - s[ok].max()) < 1.0e-3
            okq = inside & np.isfinite(q)
            assert abs(result.numbers["rough_mean"][br, bc] - q[okq].astype(np.float64).mean()) < 1.0e-3
            assert abs(result.numbers["elev_min"][br, bc] - e[inside].min()) < 1.0e-3
            assert abs(result.numbers["elev_max"][br, bc] - e[inside].max()) < 1.0e-3
            assert abs(result.numbers["pct_any"][br, bc] - 100.0 * (g == 1).sum() / inside.sum()) < 1.0e-6
            assert abs(result.numbers["pct_upslope"][br, bc] - 100.0 * (g == 2).sum() / inside.sum()) < 1.0e-6
            assert abs(result.numbers["pct_cover"][br, bc] - 100.0 * inside.sum() / g.size) < 1.0e-6
    # The rectangles tile the window exactly, clip at its edge and carry every field.
    rows_out = list(result.rows())
    assert len(rows_out) == result.cell_count
    first_ring = rows_out[0][1]
    assert first_ring[0] == first_ring[-1] and len(first_ring) == 5
    xs = [p[0] for _, ring, _ in rows_out for p in ring]
    ys = [p[1] for _, ring, _ in rows_out for p in ring]
    assert min(xs) == 0.0 and max(xs) == float(cols) and max(ys) == 1000.0 and min(ys) == 1000.0 - rows
    assert {"Cell_ID", "Pct_Pass", "Terr_Class", "Pass_Class", "Cand_Count", "Best_Rating"} <= set(rows_out[0][2])


def test_overview_tile_with_no_valid_cell_returns_none() -> None:
    plan = make_plan((0, 64, 0, 64), 1.0, 32.0)
    ground = np.full((64, 64), 255, dtype=np.uint8)
    nothing = np.zeros((64, 64), dtype=np.float32)
    assert tile_partial(plan, 0, 64, 0, 64, ground, nothing, nothing, nothing) is None
    accumulator = OverviewAccumulator(plan)
    accumulator.add(None)
    assert finalize(accumulator, GridGeometry(0.0, 64.0, 1.0, -1.0, 64, 64), 7.0).cell_count == 0


def test_overview_stale_partial_is_refused() -> None:
    plan = make_plan((0, 64, 0, 64), 1.0, 32.0)
    accumulator = OverviewAccumulator(plan)
    try:
        accumulator.add({"br0": 5, "bc0": 0, "shape": [2, 2]})
    except OverviewError:
        return
    raise AssertionError("a partial outside the grid must be refused")


# --- overview grid: whole pipeline -----------------------------------------------------------


class _OverviewWriter(_NullWriter):
    def write_overview(self, overview):  # type: ignore[no-untyped-def]
        self.overview = overview
        return {"overview_grid": "overview"}


def _run_overview(data, max_input_cells: int, cell_m: float | None = 100.0, folder=None, **kwargs):  # type: ignore[no-untyped-def]
    from hlzcore.pipeline import ArrayTileReader, RunOptions, run_tiled_pipeline

    mission = replace(MISSION, max_input_cells=max_input_cells, max_candidates=5000, max_candidates_per_tile=500)
    writer = _OverviewWriter()
    result = run_tiled_pipeline(
        ArrayTileReader(data), TILE_AIRCRAFT, mission, APPROVED, RunOptions(overview_cell_m=cell_m),
        folder or Path(tempfile.mkdtemp()), writer, **kwargs,
    )
    return result, writer


def test_overview_off_by_default() -> None:
    from test_core import _run

    result, _ = _run(_scene(), 10_000_000)
    assert result.metadata["overview"] is None and "overview_grid" not in result.outputs


def test_overview_agrees_with_the_ground_class_raster_and_the_candidates() -> None:
    data = _scene()
    result, writer = _run_overview(data, 250_000)
    assert result.metadata["tiling"]["tile_count"] >= 4
    overview = writer.overview
    ground = writer.arrays["Ground_Class"]
    block = overview.plan.block
    assert block == 100 and result.outputs["overview_grid"] == "overview"
    for br in range(overview.plan.n_rows):
        for bc in range(overview.plan.n_cols):
            g = ground[br * block : (br + 1) * block, bc * block : (bc + 1) * block]
            inside = g != 255
            if not inside.any():
                continue
            assert abs(overview.numbers["pct_any"][br, bc] - 100.0 * (g == 1).sum() / inside.sum()) < 1.0e-6
            assert abs(overview.numbers["pct_upslope"][br, bc] - 100.0 * (g == 2).sum() / inside.sum()) < 1.0e-6
    # Every ranked candidate is counted in exactly one overview cell.
    assert int(overview.candidate_count.sum()) == len(result.candidates)
    for candidate in result.candidates[:20]:
        assert overview.candidate_count[candidate.row // block, candidate.col // block] > 0
    best = overview.best_rating[result.candidates[0].row // block, result.candidates[0].col // block]
    assert best == result.candidates[0].rating
    meta = result.metadata["overview"]
    assert meta["cells"] == overview.cell_count and sum(meta["pass_class_counts"].values()) == overview.cell_count


def test_overview_identical_for_single_tile_and_tiled_runs() -> None:
    data = _scene()
    _, single = _run_overview(data, 10_000_000)
    tiled_result, tiled = _run_overview(data, 250_000)
    assert tiled_result.metadata["tiling"]["tile_count"] >= 4
    for key, values in single.overview.numbers.items():
        assert np.allclose(values, tiled.overview.numbers[key], equal_nan=True, atol=1.0e-3), key
    assert np.array_equal(single.overview.terrain, tiled.overview.terrain)
    assert np.array_equal(single.overview.passing, tiled.overview.passing)


def test_overview_survives_an_interrupted_run() -> None:
    from hlzcore.pipeline import AnalysisCancelled

    data = _scene()
    reference_result, reference = _run_overview(data, 250_000)
    folder = Path(tempfile.mkdtemp())
    try:
        _run_overview(data, 250_000, folder=folder, stop_after_tiles=2)
    except AnalysisCancelled:
        pass
    else:
        raise AssertionError("the test hook should stop the run")
    resumed_result, resumed = _run_overview(data, 250_000, folder=folder)
    assert resumed_result.metadata["resumed"] is True
    for key, values in reference.overview.numbers.items():
        assert np.allclose(values, resumed.overview.numbers[key], equal_nan=True, atol=1.0e-3), key
    assert np.array_equal(reference.overview.candidate_count, resumed.overview.candidate_count)


def test_overview_respects_the_aoi_and_the_minimum_block() -> None:
    data = _scene()
    mask = np.zeros(data.dtm.shape, dtype=bool)
    mask[100:300, 100:340] = True
    data.candidate_mask = mask
    result, writer = _run_overview(data, 10_000_000, cell_m=50.0)
    overview = writer.overview
    assert overview.plan.window == (100, 300, 100, 340)
    assert (overview.plan.n_rows, overview.plan.n_cols) == (4, 5)
    assert int(overview.numbers["cells_valid"][overview.valid].sum()) == int(mask.sum())
    # The smallest allowed request (20 m) on a 1 m DTM is 20 cells, above the 16-cell minimum, so it is kept.
    _, small_writer = _run_overview(_scene(), 10_000_000, cell_m=20.0)
    assert small_writer.overview.plan.block == 20 and not small_writer.overview.plan.adjusted


def test_overview_bad_cell_size_is_a_clear_error() -> None:
    from hlzcore.pipeline import ArrayTileReader, RunOptions, run_tiled_pipeline

    try:
        run_tiled_pipeline(
            ArrayTileReader(_scene()), TILE_AIRCRAFT, MISSION, APPROVED, RunOptions(overview_cell_m=5.0),
            Path(tempfile.mkdtemp()), _OverviewWriter(),
        )
    except OverviewError as error:
        assert "between" in str(error)
        return
    raise AssertionError("a 5 m overview cell must be refused")


def test_overview_writer_without_support_does_not_break_the_run() -> None:
    from hlzcore.pipeline import ArrayTileReader, RunOptions, run_tiled_pipeline

    result = run_tiled_pipeline(
        ArrayTileReader(_scene()), TILE_AIRCRAFT, MISSION, APPROVED, RunOptions(overview_cell_m=100.0),
        Path(tempfile.mkdtemp()), _NullWriter(),
    )
    assert "overview_grid" not in result.outputs and result.candidates
    assert any("cannot write an overview grid" in warning for warning in result.metadata["warnings"])


def test_overview_geojson_writer() -> None:
    """The command-line writer produces a valid GeoJSON polygon layer (needs rasterio)."""
    try:
        import rasterio  # noqa: F401
    except ImportError:
        return
    from hlzcore.io_rasterio import RasterioWriter

    data = _scene()
    _, writer = _run_overview(data, 10_000_000)
    folder = Path(tempfile.mkdtemp())
    geo = RasterioWriter(folder, "EPSG:32618", {})
    geo.crs = "EPSG:32618"
    grid = writer.overview.grid
    # The synthetic geometry sits at the origin; shift it into UTM 18N so the WGS84 conversion is meaningful.
    shifted = replace(writer.overview, grid=GridGeometry(445000.0, 5032400.0, grid.pixel_width, grid.pixel_height, grid.width, grid.height))
    paths = geo.write_overview(shifted)
    document = json.loads(Path(paths["overview_grid"]).read_text(encoding="utf-8"))
    assert document["type"] == "FeatureCollection" and len(document["features"]) == shifted.cell_count
    feature = document["features"][0]
    assert feature["geometry"]["type"] == "Polygon" and "Pct_Pass" in feature["properties"]


# --- toolbox dialog (v0.7.0) ----------------------------------------------------------------


def test_toolbox_point_and_overview_fields() -> None:
    """The dialog lists point layers, enables the overview size only when asked, and still refuses other shapes."""
    import subprocess
    import textwrap

    script = textwrap.dedent(
        """
        import importlib.machinery, importlib.util, sys
        from pathlib import Path
        from types import SimpleNamespace
        root = Path(sys.argv[1])
        sys.path[:0] = [str(root / "tests" / "fake_arcpy"), str(root)]
        import arcpy
        loader = importlib.machinery.SourceFileLoader("hlz_pyt", str(root / "toolbox" / "HLZ_Suitability.pyt"))
        spec = importlib.util.spec_from_loader("hlz_pyt", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        tool = module.AnalyzeHLZ()
        params = tool.getParameterInfo()
        assert len(params) == 43 and params[module.P_OUT_REPORT].name == "out_report"
        assert (module.P_POINT_RADIUS, module.P_OVERVIEW, module.P_OVERVIEW_SIZE) == (32, 33, 34)
        assert params[module.P_POINT_RADIUS].filter.list == [0, 500] and params[module.P_OVERVIEW_SIZE].filter.list == [20, 20000]
        arcpy.Describe = lambda layer: SimpleNamespace(shapeType="Point", extent=None, spatialReference=None)
        params[module.P_EXCLUSION].value = "hazards"
        params[module.P_EXCLUSION].values = ["hazards"]
        params[module.P_OVERVIEW].value = True
        tool.updateParameters(params)
        assert params[module.P_OVERVIEW_SIZE].enabled is True
        params[module.P_OVERVIEW].value = False
        tool.updateParameters(params)
        assert params[module.P_OVERVIEW_SIZE].enabled is False
        tool.updateMessages(params)
        assert not params[module.P_EXCLUSION].hasError()
        assert "point layer" in (params[module.P_POINT_RADIUS].message or "")
        arcpy.Describe = lambda layer: SimpleNamespace(shapeType="MultiPatch", extent=None, spatialReference=None)
        params = tool.getParameterInfo()
        params[module.P_EXCLUSION].value = "bad"
        params[module.P_EXCLUSION].values = ["bad"]
        tool.updateMessages(params)
        assert params[module.P_EXCLUSION].hasError()
        print("TOOLBOX_POINTS_OK")
        """
    )
    completed = subprocess.run([sys.executable, "-c", script, str(ROOT)], capture_output=True, text=True, timeout=120)
    assert "TOOLBOX_POINTS_OK" in completed.stdout, completed.stdout[-1500:] + completed.stderr[-2500:]
