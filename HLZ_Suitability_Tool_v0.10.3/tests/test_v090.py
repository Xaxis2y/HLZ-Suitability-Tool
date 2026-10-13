# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Unit tests for v0.9.0: candidate spacing and sites, automatic UTM projection, custom landing-point size."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from hlzcore.config import ProfileError, override_aircraft  # noqa: E402
from hlzcore.models import Candidate  # noqa: E402
from hlzcore.reproject import ReprojectError, metres_per_degree, square_cell_m, utm_epsg, utm_zone  # noqa: E402
from hlzcore.spacing import site_groups, spacing_summary, thin_by_spacing  # noqa: E402
from test_core import APPROVED, MISSION, TILE_AIRCRAFT, _NullWriter, _run, _scene  # noqa: E402


def _cand(index: int, x: float, y: float) -> Candidate:
    return Candidate(candidate_id=f"C{index}", row=0, col=0, x=x, y=y, elevation_m=0.0, slope_deg=0.0, aspect_deg=None,
                     roughness_m=0.0, max_tdp_obstacle_m=None, max_ring_obstacle_m=None, ring_coverage_fraction=1.0,
                     component_area_m2=0.0)


# --- spacing and sites -----------------------------------------------------------------------------


def test_thin_by_spacing_keeps_the_better_candidate() -> None:
    ranked = [_cand(0, 0, 0), _cand(1, 30, 0), _cand(2, 120, 0), _cand(3, 130, 0), _cand(4, 400, 0)]
    kept, dropped = thin_by_spacing(ranked, 100.0)
    assert [c.candidate_id for c in kept] == ["C0", "C2", "C4"] and dropped == 2
    # A dropped candidate does not drop others (greedy in rank order): C1 is gone but C2 survives next to C3 only if far enough.
    kept, dropped = thin_by_spacing(ranked, 10.0)
    assert len(kept) == 5 and dropped == 0
    kept, dropped = thin_by_spacing(ranked, 0.0)
    assert len(kept) == 5 and dropped == 0
    one, none = thin_by_spacing([_cand(0, 0, 0)], 100.0)
    assert len(one) == 1 and none == 0 and thin_by_spacing([], 50.0) == ([], 0)
    # Exactly at the spacing is kept (strictly closer is dropped).
    kept, _ = thin_by_spacing([_cand(0, 0, 0), _cand(1, 100, 0)], 100.0)
    assert len(kept) == 2


def test_thin_by_spacing_matches_brute_force() -> None:
    rng = np.random.default_rng(3)
    xy = rng.uniform(0, 1000, size=(300, 2))
    ranked = [_cand(i, float(x), float(y)) for i, (x, y) in enumerate(xy)]
    kept, dropped = thin_by_spacing(ranked, 80.0)
    expected: list[int] = []
    for i, (x, y) in enumerate(xy):
        if all(np.hypot(x - xy[j][0], y - xy[j][1]) >= 80.0 - 1.0e-9 for j in expected):
            expected.append(i)
    assert [int(c.candidate_id[1:]) for c in kept] == expected and dropped == 300 - len(expected)


def test_site_groups_chain_and_order() -> None:
    chain = [_cand(0, 0, 0), _cand(1, 50, 0), _cand(2, 100, 0), _cand(3, 1000, 0), _cand(4, 1040, 0), _cand(5, 5000, 5000)]
    groups = site_groups(chain, 60.0)
    assert groups.tolist() == [0, 0, 0, 1, 1, 2]  # chains merge; numbered by first appearance
    assert site_groups(chain, 0.0).tolist() == [0, 1, 2, 3, 4, 5] and site_groups([], 50.0).size == 0
    summary = spacing_summary(chain, 60.0, top_n=3)
    assert summary == {"link_m": 60.0, "candidates": 6, "groups": 3, "top_n": 3, "top_groups": 1}


def test_pipeline_sites_and_min_spacing() -> None:
    from hlzcore.pipeline import ArrayTileReader, RunOptions, run_tiled_pipeline

    mission = replace(MISSION, max_input_cells=10_000_000, max_candidates=5000, max_candidates_per_tile=500)

    def run(spacing: float):  # type: ignore[no-untyped-def]
        return run_tiled_pipeline(ArrayTileReader(_scene()), TILE_AIRCRAFT, mission, APPROVED,
                                  RunOptions(min_spacing_m=spacing), Path(tempfile.mkdtemp()), _NullWriter())

    base = run(0.0)
    sites = base.metadata["sites"]
    assert sites["candidates"] == len(base.candidates) and sites["groups"] >= 1
    assert all(c.site_no >= 1 for c in base.candidates) and base.candidates[0].site_no == 1
    assert max(c.site_no for c in base.candidates) == sites["groups"]
    spaced = run(150.0)
    assert len(spaced.candidates) < len(base.candidates)
    assert spaced.metadata["rejections"]["candidates"]["dropped_by_spacing"] == len(base.candidates) - len(spaced.candidates)
    pts = np.array([[c.x, c.y] for c in spaced.candidates])
    for i in range(len(pts)):
        for j in range(i):
            assert np.hypot(*(pts[i] - pts[j])) >= 150.0 - 1.0e-6
    assert [c.rank for c in spaced.candidates] == list(range(1, len(spaced.candidates) + 1))
    assert spaced.candidates[0].score == base.candidates[0].score  # the best candidate survives
    assert "dropped for the minimum spacing" in Path(spaced.outputs["report"]).read_text(encoding="utf-8")
    csv_rows = list(csv.DictReader(Path(spaced.outputs["csv"]).open(encoding="utf-8-sig", newline="")))
    assert "Site_No" in csv_rows[0] and csv_rows[0]["Site_No"] == "1"
    for bad in (-1.0, float("nan"), 6000.0):
        try:
            run(bad)
        except Exception as error:  # noqa: BLE001
            assert "between 0 and 5,000" in str(error)
        else:
            raise AssertionError("a bad spacing must be refused")


def test_min_spacing_is_not_part_of_the_resume_signature() -> None:
    from hlzcore.pipeline import ArrayTileReader, RunOptions, run_tiled_pipeline

    mission = replace(MISSION, max_input_cells=250_000, max_candidates=5000, max_candidates_per_tile=500)
    folder = Path(tempfile.mkdtemp())
    try:
        run_tiled_pipeline(ArrayTileReader(_scene()), TILE_AIRCRAFT, mission, APPROVED, RunOptions(), folder, _NullWriter(), stop_after_tiles=2)
    except Exception as error:  # noqa: BLE001
        assert "Stopped early" in str(error)
    resumed = run_tiled_pipeline(ArrayTileReader(_scene()), TILE_AIRCRAFT, mission, APPROVED, RunOptions(min_spacing_m=120.0),
                                 folder, _NullWriter())
    assert resumed.metadata["resumed"] is True and resumed.metadata["rejections"]["candidates"]["dropped_by_spacing"] > 0


# --- projection helpers ----------------------------------------------------------------------------


def test_utm_zone_and_epsg() -> None:
    assert utm_zone(-75.7) == 18 and utm_zone(-180.0) == 1 and utm_zone(179.99) == 60 and utm_zone(0.0) == 31
    assert utm_epsg(-75.7, 45.4, 4326)[0] == 32618
    assert utm_epsg(-75.7, 45.4, 4269) == (26918, "NAD83 / UTM zone 18N")
    assert utm_epsg(-75.7, 45.4, 4617)[0] == 26918            # NAD83(CSRS)
    assert utm_epsg(-75.7, 45.4, None, "D_North_American_1983")[0] == 26918
    assert utm_epsg(-75.7, -45.4, 4269)[0] == 32718            # no NAD83 UTM south
    assert utm_epsg(151.2, -33.9, 4326)[0] == 32756            # Sydney
    assert utm_epsg(-170.0, 60.0, 4269)[0] == 26902            # -170 deg lies in UTM zone 2 (NAD83, north)
    for lon, lat in ((10.0, 85.0), (10.0, -81.0)):
        try:
            utm_epsg(lon, lat)
        except ReprojectError as error:
            assert "UTM range" in str(error)
        else:
            raise AssertionError("polar latitudes have no UTM zone")


def test_square_cell_size() -> None:
    lon_m, lat_m = metres_per_degree(45.0)
    assert abs(lat_m - 111131.7) < 5.0 and abs(lon_m - 78846.0) < 15.0
    cell = square_cell_m(1.0e-5, 1.0e-5, 45.0)
    assert abs(cell - (lon_m * lat_m) ** 0.5 * 1.0e-5) < 0.002 and 0.9 < cell < 1.0
    assert square_cell_m(-1.0e-5, -1.0e-5, 45.0) == cell


# --- custom landing-point size ---------------------------------------------------------------------


def test_override_aircraft() -> None:
    same = override_aircraft(TILE_AIRCRAFT)
    assert same is TILE_AIRCRAFT
    bigger = override_aircraft(TILE_AIRCRAFT, 40.0, 12.0)
    assert bigger.tdp_diameter_m == 40.0 and bigger.tdp_radius_m == 20.0 and bigger.cleared_ring_m == 12.0
    assert "custom size" in bigger.display_name and bigger.profile_id.endswith("+custom_size")
    assert bigger.max_roughness_m == TILE_AIRCRAFT.max_roughness_m and bigger.approach_range_m == TILE_AIRCRAFT.approach_range_m
    only_ring = override_aircraft(TILE_AIRCRAFT, None, 0.0)
    assert only_ring.cleared_ring_m == 0.0 and only_ring.tdp_diameter_m == TILE_AIRCRAFT.tdp_diameter_m
    for diameter, ring in ((4.0, None), (201.0, None), (None, -1.0), (None, 201.0)):
        try:
            override_aircraft(TILE_AIRCRAFT, diameter, ring)
        except ProfileError:
            pass
        else:
            raise AssertionError(f"({diameter}, {ring}) must be refused")


def test_bigger_landing_point_finds_fewer_or_equal_sites() -> None:
    from hlzcore.pipeline import ArrayTileReader, RunOptions, run_tiled_pipeline

    mission = replace(MISSION, max_input_cells=10_000_000, max_candidates=5000, max_candidates_per_tile=500)

    def run(aircraft):  # type: ignore[no-untyped-def]
        return run_tiled_pipeline(ArrayTileReader(_scene()), aircraft, mission, APPROVED, RunOptions(), Path(tempfile.mkdtemp()), _NullWriter())

    base = run(TILE_AIRCRAFT)
    large = run(override_aircraft(TILE_AIRCRAFT, TILE_AIRCRAFT.tdp_diameter_m * 2.0))
    assert len(large.candidates) <= len(base.candidates)
    assert large.metadata["profiles"]["aircraft"]["tdp_diameter_m"] == TILE_AIRCRAFT.tdp_diameter_m * 2.0
    assert "custom size" in large.metadata["profiles"]["aircraft"]["display_name"]


# --- automatic projection of a latitude/longitude DTM (command line) --------------------------------


def _geographic_dtm(folder: Path, epsg: str = "EPSG:4269", lat0: float = 45.43, lon0: float = -75.70):  # type: ignore[no-untyped-def]
    import rasterio
    from rasterio.transform import from_origin

    n = 700
    cell = 1.0e-5
    dem = np.full((n, n), 70.0, dtype="float32")
    cols = np.arange(n)[None, :] * np.ones((n, 1))
    dem[:, 400:] += (cols[:, 400:] - 400) * 0.7  # a steep ramp on the east third
    path = folder / "dtm_ll.tif"
    with rasterio.open(path, "w", driver="GTiff", width=n, height=n, count=1, dtype="float32", crs=epsg,
                       transform=from_origin(lon0, lat0, cell, cell), nodata=-9999.0) as dataset:
        dataset.write(dem, 1)
    return path


def test_geographic_dtm_is_projected_on_the_fly() -> None:
    try:
        import rasterio  # noqa: F401
    except ImportError:
        return
    from hlzcore.cli import main

    folder = Path(tempfile.mkdtemp())
    path = _geographic_dtm(folder)
    code = main(["analyze", "--dtm", str(path), "--output", str(folder / "out"), "--run-name", "ll"])
    assert code == 0
    meta = json.loads((folder / "out" / "ll" / "run_metadata.json").read_text(encoding="utf-8"))
    assert meta["inputs"]["crs"] == "EPSG:26918" and "reprojected to EPSG:26918" in meta["inputs"]["dtm"]
    assert 0.9 < meta["cell_size_m"] < 1.0
    assert any("reprojected on the fly" in w for w in meta["warnings"])
    top = meta["candidates"][0]
    assert 45.42 < top["latitude"] < 45.431 and -75.70 < top["longitude"] < -75.69
    assert top["mgrs"].startswith("18T") and top["rating"]
    # The steep ramp (35 degrees) never carries a candidate: x east of column 400.
    east_edge = -75.70 + 400 * 1.0e-5
    assert all(c["longitude"] < east_edge + 0.0002 for c in meta["candidates"] if c["rating"] in ("Highly suitable", "Suitable"))
    # WGS 84 gives zone 18 north with the WGS 84 datum.
    path2 = _geographic_dtm(folder, epsg="EPSG:4326")
    assert main(["analyze", "--dtm", str(path2), "--output", str(folder / "out"), "--run-name", "ll84"]) == 0
    assert json.loads((folder / "out" / "ll84" / "run_metadata.json").read_text(encoding="utf-8"))["inputs"]["crs"] == "EPSG:32618"


def test_geographic_dtm_refused_without_auto_reproject() -> None:
    try:
        import rasterio  # noqa: F401
    except ImportError:
        return
    from hlzcore.cli import main

    folder = Path(tempfile.mkdtemp())
    path = _geographic_dtm(folder)
    code = main(["analyze", "--dtm", str(path), "--output", str(folder / "out"), "--run-name", "no", "--no-auto-reproject"])
    assert code != 0


def test_geographic_dtm_with_a_geographic_dsm() -> None:
    try:
        import rasterio  # noqa: F401
    except ImportError:
        return
    from hlzcore.io_rasterio import RasterioTileReader

    folder = Path(tempfile.mkdtemp())
    dtm = _geographic_dtm(folder)
    dsm = folder / "dsm_ll.tif"
    import rasterio

    with rasterio.open(dtm) as source:
        data = source.read(1) + 12.0
        profile = source.profile
    with rasterio.open(dsm, "w", **profile) as dataset:
        dataset.write(data, 1)
    reader = RasterioTileReader(dtm, dsm)
    try:
        assert reader.reprojection and reader.has_dsm
        dtm_a, dtm_valid, dsm_a, dsm_valid = reader.read(100, 140, 100, 140)
        assert dtm_valid.all() and dsm_valid.all()
        assert abs(float(np.nanmean(dsm_a - dtm_a)) - 12.0) < 0.05
    finally:
        reader.close()


# --- toolbox -----------------------------------------------------------------------------------------


def test_toolbox_v090_fields() -> None:
    script = textwrap.dedent(
        """
        import importlib.machinery, importlib.util, sys
        from pathlib import Path
        root = Path(sys.argv[1])
        sys.path[:0] = [str(root / "tests" / "fake_arcpy"), str(root)]
        import arcpy
        loader = importlib.machinery.SourceFileLoader("hlz_pyt", str(root / "toolbox" / "HLZ_Suitability.pyt"))
        spec = importlib.util.spec_from_loader("hlz_pyt", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        tool = module.AnalyzeHLZ()
        params = tool.getParameterInfo()
        assert len(params) == 43
        assert (module.P_TDP_OVERRIDE, module.P_RING_OVERRIDE, module.P_MIN_SPACING, module.P_AUTO_PROJECT) == (37, 38, 39, 40)
        assert (module.P_OUT_FC, module.P_OUT_REPORT) == (41, 42)
        assert [params[i].name for i in range(37, 43)] == ["tdp_diameter_override_m", "cleared_ring_override_m", "min_spacing_m", "auto_project", "out_candidates", "out_report"]
        assert params[module.P_AUTO_PROJECT].value is True and params[module.P_MIN_SPACING].value == 0.0
        assert params[module.P_TDP_OVERRIDE].filter.list == [5, 200] and params[module.P_RING_OVERRIDE].filter.list == [0, 200]
        assert module._override_value(params[module.P_TDP_OVERRIDE]) is None
        params[module.P_TDP_OVERRIDE].value = 40.0
        assert module._override_value(params[module.P_TDP_OVERRIDE]) == 40.0
        aircraft = module.load_aircraft_profile(sorted(module._aircraft_choices().values())[0])
        changed = module._with_size_overrides(params, aircraft)
        assert changed.tdp_diameter_m == 40.0 and "custom size" in changed.display_name
        params[module.P_TDP_OVERRIDE].value = 3.0
        try:
            module._with_size_overrides(params, aircraft)
        except module.ProfileError:
            pass
        else:
            raise AssertionError("3 m must be refused")
        print("TOOLBOX_V090_OK")
        """
    )
    completed = subprocess.run([sys.executable, "-c", script, str(ROOT)], capture_output=True, text=True, timeout=120)
    assert "TOOLBOX_V090_OK" in completed.stdout, completed.stdout[-1500:] + completed.stderr[-2500:]


def test_dtm_problems_allow_geographic() -> None:
    # dtm_problems lives in io_arcpy (needs arcpy); read its source to test the contract without ArcPy.
    source = (ROOT / "hlzcore" / "io_arcpy.py").read_text(encoding="utf-8")
    assert "def dtm_problems(info: dict[str, Any], allow_geographic: bool = False)" in source
    assert 'info["sr_type"] == "Geographic" and allow_geographic' in source
    assert "def project_geographic_raster(" in source and "ProjectRaster(raster, out_path" in source


def test_help_text_covers_the_v090_fields() -> None:
    from hlzcore.help_text import FIELD_HELP, guidance

    for name in ("tdp_diameter_override_m", "cleared_ring_override_m", "min_spacing_m", "auto_project"):
        assert name in FIELD_HELP and all(k in FIELD_HELP[name] for k in ("title", "what", "requires", "effect")), name
    assert "40" in guidance("tdp_diameter_override_m", {"tdp_override": 40.0, "ring_effective": 25.0})
    assert "150" in guidance("min_spacing_m", {"min_spacing": 150})
    assert "stop" in guidance("auto_project", {"dtm_geographic": True, "auto_project": False})
