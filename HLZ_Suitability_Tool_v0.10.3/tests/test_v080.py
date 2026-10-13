# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Unit tests for v0.8.0: rejection summary, dual units, drawn area of interest. Needs only NumPy and SciPy."""

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

from hlzcore import rejections  # noqa: E402
from hlzcore.report import CSV_COLUMNS, write_candidate_csv  # noqa: E402
from hlzcore.terrain import (  # noqa: E402
    GROUND_FAIL,
    GROUND_PASS,
    GROUND_UPSLOPE_ONLY,
    REASON_KEYS,
    REASON_PASS_ANY,
    REASON_PASS_UPSLOPE,
    analyze_terrain,
    ground_class_raster,
    ground_reason_codes,
)
from hlzcore.units import M_TO_FT, fmt_length, validate_units  # noqa: E402
from test_core import APPROVED, MISSION, TILE_AIRCRAFT, _NullWriter, _run, _scene  # noqa: E402


# --- units ---------------------------------------------------------------------------------------


def test_units_formatting() -> None:
    assert abs(M_TO_FT - 3.280839895) < 1.0e-8
    assert fmt_length(100.0, 1, "METRIC") == "100.0 m"
    assert fmt_length(100.0, 1, "BOTH") == "100.0 m (328.1 ft)"
    assert fmt_length(100.0, 1, "IMPERIAL") == "328.1 ft (100.0 m)"
    assert fmt_length(1234.5, 0, "BOTH") == "1,234 m (4,050 ft)"
    assert fmt_length(None) == "" and fmt_length(float("nan")) == "" and fmt_length(float("inf")) == ""
    assert validate_units(None) == "BOTH" and validate_units("feet") == "IMPERIAL" and validate_units("metres") == "METRIC"
    try:
        validate_units("furlongs")
    except ValueError as error:
        assert "furlongs" in str(error)
    else:
        raise AssertionError("an unknown unit must be refused")


def test_csv_has_feet_columns_appended_and_consistent() -> None:
    result, _ = _run(_scene(), 10_000_000)
    folder = Path(tempfile.mkdtemp())
    path = write_candidate_csv(folder / "c.csv", result.candidates)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    headers = list(rows[0])
    # The v0.7.x columns keep their positions; the feet columns come after them.
    assert headers[: len(CSV_COLUMNS) - 8] == [h for h, _ in CSV_COLUMNS[:-8]]  # v0.9.0 appended Site_No after the feet columns
    for name in ("Elevation_ft", "Roughness_ft", "Max_obstacle_in_TDP_ft", "Max_obstacle_in_ring_ft",
                 "Distance_to_exclusion_ft", "Nearest_obstacle_ft", "Distance_from_centre_ft"):
        assert name in headers[-11:-4], name
    first = rows[0]
    assert abs(float(first["Elevation_ft"]) - float(first["Elevation_m"]) * M_TO_FT) < 0.01
    assert abs(float(first["Roughness_ft"]) - float(first["Roughness_m"]) * M_TO_FT) < 0.01


def test_candidate_feet_properties() -> None:
    result, _ = _run(_scene(), 10_000_000)
    candidate = result.candidates[0]
    assert abs(candidate.elevation_ft - candidate.elevation_m / 0.3048) < 1.0e-9
    assert candidate.excluded_distance_ft is None and candidate.nearest_obstacle_ft is None
    candidate.nearest_obstacle_m = 30.48
    assert abs(candidate.nearest_obstacle_ft - 100.0) < 1.0e-9


def test_report_units_modes() -> None:
    from hlzcore.pipeline import ArrayTileReader, RunOptions, run_tiled_pipeline

    mission = replace(MISSION, max_input_cells=10_000_000, max_candidates=5000, max_candidates_per_tile=500)
    texts = {}
    for mode in ("METRIC", "BOTH", "IMPERIAL"):
        folder = Path(tempfile.mkdtemp())
        result = run_tiled_pipeline(
            ArrayTileReader(_scene()), TILE_AIRCRAFT, mission, APPROVED, RunOptions(report_units=mode), folder, _NullWriter()
        )
        assert result.metadata["options"]["report_units"] == mode
        texts[mode] = Path(result.outputs["report"]).read_text(encoding="utf-8")
    assert " ft)" not in texts["METRIC"].split("Top ")[1].split("Warnings")[0]
    assert " ft)" in texts["BOTH"] and "Elevation " in texts["BOTH"]
    imperial_detail = texts["IMPERIAL"].split("Top ")[1].split("Warnings")[0]
    assert " ft (" in imperial_detail
    try:
        run_tiled_pipeline(ArrayTileReader(_scene()), TILE_AIRCRAFT, mission, APPROVED, RunOptions(report_units="x"),
                           Path(tempfile.mkdtemp()), _NullWriter())
    except ValueError:
        pass
    else:
        raise AssertionError("bad units must be refused")


def test_report_units_do_not_change_the_resume_signature() -> None:
    from hlzcore.pipeline import ArrayTileReader, RunOptions, run_tiled_pipeline

    mission = replace(MISSION, max_input_cells=250_000, max_candidates=5000, max_candidates_per_tile=500)
    folder = Path(tempfile.mkdtemp())
    try:
        run_tiled_pipeline(ArrayTileReader(_scene()), TILE_AIRCRAFT, mission, APPROVED, RunOptions(report_units="METRIC"),
                           folder, _NullWriter(), stop_after_tiles=2)
    except Exception as error:  # noqa: BLE001
        assert "Stopped early" in str(error)
    resumed = run_tiled_pipeline(ArrayTileReader(_scene()), TILE_AIRCRAFT, mission, APPROVED, RunOptions(report_units="IMPERIAL"),
                                 folder, _NullWriter())
    assert resumed.metadata["resumed"] is True


# --- rejection reasons -----------------------------------------------------------------------------


def _terrain_and_args(obstacles: bool):  # type: ignore[no-untyped-def]
    data = _scene()
    dtm_valid = np.isfinite(data.dtm)
    dsm = data.dsm if obstacles else None
    terrain = analyze_terrain(
        dtm=data.dtm, dtm_valid=dtm_valid, dsm=dsm, dsm_valid=None if dsm is None else np.isfinite(dsm),
        cell_size_m=data.geometry.cell_size_m, tdp_radius_m=TILE_AIRCRAFT.tdp_radius_m,
        cleared_ring_m=TILE_AIRCRAFT.cleared_ring_m, tile_target_cells=MISSION.tile_target_cells,
    )
    args = dict(
        max_slope_deg=TILE_AIRCRAFT.max_landing_slope_deg, max_upslope_deg=TILE_AIRCRAFT.max_upslope_landing_deg,
        max_roughness_m=TILE_AIRCRAFT.max_roughness_m, max_tdp_obstacle_m=TILE_AIRCRAFT.tdp_obstacle_height_m,
        max_ring_obstacle_m=TILE_AIRCRAFT.ring_obstacle_height_m, minimum_valid_fraction=MISSION.minimum_valid_fraction,
        minimum_ring_coverage=MISSION.minimum_ring_coverage_fraction, obstacle_evidence=obstacles,
    )
    return terrain, dtm_valid, args


def test_reason_codes_agree_with_the_ground_class_raster() -> None:
    for obstacles in (False, True):
        terrain, dtm_valid, args = _terrain_and_args(obstacles)
        classes = ground_class_raster(terrain, dtm_valid, **args)
        reasons = ground_reason_codes(terrain, dtm_valid, **args)
        assert np.array_equal(reasons == REASON_PASS_ANY, classes == GROUND_PASS)
        assert np.array_equal(reasons == REASON_PASS_UPSLOPE, classes == GROUND_UPSLOPE_ONLY)
        failing = (classes == GROUND_FAIL)
        assert np.all(reasons[failing] >= 2) and np.all(reasons[failing] != 255)
        assert np.all(reasons[~dtm_valid] == 255)
    # With a DSM the scene fails for several different reasons (coverage and obstacle heights as well as missing data).
    terrain, dtm_valid, args = _terrain_and_args(True)
    reasons = ground_reason_codes(terrain, dtm_valid, **args)
    assert len(set(np.unique(reasons[reasons >= 2][reasons[reasons >= 2] != 255]).tolist())) >= 2


def test_slope_and_roughness_reasons_on_synthetic_terrain() -> None:
    rows = cols = 150
    rng = np.random.default_rng(7)
    x = np.arange(cols, dtype=float)[None, :] * np.ones((rows, 1))
    dtm = np.zeros((rows, cols))
    dtm[:, 50:100] = 0.7 * (x[:, 50:100] - 50)                    # a steep ramp (35 deg)
    dtm[:, 100:] = rng.normal(0.0, 1.5, size=(rows, cols - 100))   # rough ground
    valid = np.ones((rows, cols), dtype=bool)
    terrain = analyze_terrain(dtm=dtm, dtm_valid=valid, dsm=None, dsm_valid=None, cell_size_m=1.0,
                              tdp_radius_m=TILE_AIRCRAFT.tdp_radius_m, cleared_ring_m=TILE_AIRCRAFT.cleared_ring_m,
                              tile_target_cells=MISSION.tile_target_cells)
    args = dict(
        max_slope_deg=TILE_AIRCRAFT.max_landing_slope_deg, max_upslope_deg=TILE_AIRCRAFT.max_upslope_landing_deg,
        max_roughness_m=TILE_AIRCRAFT.max_roughness_m, max_tdp_obstacle_m=TILE_AIRCRAFT.tdp_obstacle_height_m,
        max_ring_obstacle_m=TILE_AIRCRAFT.ring_obstacle_height_m, minimum_valid_fraction=MISSION.minimum_valid_fraction,
        minimum_ring_coverage=MISSION.minimum_ring_coverage_fraction, obstacle_evidence=False,
    )
    classes = ground_class_raster(terrain, valid, **args)
    reasons = ground_reason_codes(terrain, valid, **args)
    from hlzcore.terrain import REASON_ROUGHNESS, REASON_SLOPE

    assert np.array_equal(reasons == REASON_PASS_ANY, classes == GROUND_PASS)
    assert np.array_equal(reasons == REASON_PASS_UPSLOPE, classes == GROUND_UPSLOPE_ONLY)
    assert (reasons == REASON_SLOPE).sum() > 1000 and (reasons == REASON_ROUGHNESS).sum() > 500
    assert reasons[75, 75] == REASON_SLOPE and reasons[75, 130] in (REASON_ROUGHNESS, REASON_SLOPE)
    assert reasons[75, 20] == REASON_PASS_ANY


def test_tile_counts_and_summary() -> None:
    reason = np.array([[0, 0, 1], [3, 3, 4], [255, 2, 8]], dtype=np.uint8)
    inside = reason != 255
    counts = rejections.tile_counts(reason, inside)
    assert counts["pass_any"] == 2 and counts["slope"] == 2 and counts["roughness"] == 1 and counts["exclusion"] == 1
    assert sum(counts.values()) == 8 and set(counts) == set(REASON_KEYS.values())
    summary = rejections.build_summary(counts, {"exact_checks": 10, "exact_rejected": 3, "seam_duplicates_removed": 1,
                                                  "dropped_by_limit": 0, "kept": 6}, 2.0)
    assert summary["cells_evaluated"] == 8 and summary["main_reason"]["key"] == "slope"
    assert abs(sum(row["percent"] for row in summary["reasons"]) - 100.0) < 0.1
    assert summary["reasons"][0]["area_ha"] == round(2 * 4 / 1.0e4, 2)
    lines = rejections.summary_lines(summary)
    assert any("Ground too steep" in line for line in lines) and any("Candidate results" in line for line in lines)
    folder = Path(tempfile.mkdtemp())
    path = rejections.write_csv(folder / "r.csv", summary)
    text = path.read_text(encoding="utf-8-sig")
    assert "Passed_ground_test" in text and "Candidates kept" in text
    html_text = rejections.html_section(summary)
    assert "Why cells and sites were rejected" in html_text and "ground too steep" in html_text
    assert rejections.html_section(None) == "" and rejections.html_section({"cells_evaluated": 0}) == ""


def test_pipeline_rejection_summary_is_consistent() -> None:
    result, _ = _run(_scene(), 10_000_000)
    summary = result.metadata["rejections"]
    totals = result.metadata["results"]
    passed = sum(row["cells"] for row in summary["reasons"] if row["passed"])
    assert passed == totals["ground_pass_cell_count"]
    assert summary["cells_evaluated"] == totals["valid_cell_count"]
    assert summary["candidates"]["kept"] == len(result.candidates)
    assert summary["candidates"]["exact_checks"] == totals["exact_checks"]
    assert Path(result.outputs["rejection_summary"]).exists()
    assert "Why cells and sites were rejected" in Path(result.outputs["report"]).read_text(encoding="utf-8")


def test_rejection_summary_identical_for_single_tile_tiled_and_resumed_runs() -> None:
    single, _ = _run(_scene(), 10_000_000)
    tiled, _ = _run(_scene(), 250_000)
    assert tiled.metadata["tiling"]["tile_count"] > 1
    key = lambda r: {row["key"]: row["cells"] for row in r.metadata["rejections"]["reasons"]}  # noqa: E731
    assert key(single) == key(tiled)
    from hlzcore.pipeline import ArrayTileReader, RunOptions, run_tiled_pipeline

    mission = replace(MISSION, max_input_cells=250_000, max_candidates=5000, max_candidates_per_tile=500)
    folder = Path(tempfile.mkdtemp())
    try:
        run_tiled_pipeline(ArrayTileReader(_scene()), TILE_AIRCRAFT, mission, APPROVED, RunOptions(), folder, _NullWriter(), stop_after_tiles=2)
    except Exception as error:  # noqa: BLE001
        assert "Stopped early" in str(error)
    resumed = run_tiled_pipeline(ArrayTileReader(_scene()), TILE_AIRCRAFT, mission, APPROVED, RunOptions(), folder, _NullWriter())
    assert resumed.metadata["resumed"] is True and key(resumed) == key(single)


def test_exclusion_and_obstacle_reasons_are_counted() -> None:
    # An exclusion mask over part of the open ground must show up under the exclusion reason, and nowhere else.
    from hlzcore.pipeline import ArrayTileReader, RunOptions, run_tiled_pipeline

    plain, _ = _run(_scene(), 10_000_000)
    data = _scene()
    first = plain.candidates[0]
    excl = np.zeros(data.dtm.shape, dtype=bool)
    excl[max(0, first.row - 15) : first.row + 15, max(0, first.col - 15) : first.col + 15] = True
    data.exclusion_mask = excl
    mission = replace(MISSION, max_input_cells=10_000_000, max_candidates=5000, max_candidates_per_tile=500)
    result = run_tiled_pipeline(ArrayTileReader(data), TILE_AIRCRAFT, mission, APPROVED, RunOptions(),
                                Path(tempfile.mkdtemp()), _NullWriter())
    cells = {row["key"]: row["cells"] for row in result.metadata["rejections"]["reasons"]}
    base = {row["key"]: row["cells"] for row in plain.metadata["rejections"]["reasons"]}
    assert cells["exclusion"] > 0 and base["exclusion"] == 0
    assert cells["pass_any"] + cells["pass_upslope"] < base["pass_any"] + base["pass_upslope"]


# --- toolbox: drawn area and units (fake ArcPy) ------------------------------------------------------


def test_toolbox_drawn_area_and_units_fields() -> None:
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
        assert len(params) == 43
        assert (module.P_DRAWN_AOI, module.P_REPORT_UNITS, module.P_OUT_FC, module.P_OUT_REPORT) == (35, 36, 41, 42)
        assert params[module.P_DRAWN_AOI].name == "drawn_aoi" and params[module.P_DRAWN_AOI].datatype == "GPFeatureRecordSetLayer"
        assert params[module.P_REPORT_UNITS].name == "report_units" and params[module.P_REPORT_UNITS].value == "Metres and feet"
        assert params[module.P_OUT_FC].name == "out_candidates" and params[module.P_OUT_REPORT].name == "out_report"
        # Units choice mapping.
        for text, expected in (("Metres and feet", "BOTH"), ("Metres only", "METRIC"), ("Feet first (feet and metres)", "IMPERIAL")):
            params[module.P_REPORT_UNITS].value = text
            assert module._units_choice(params[module.P_REPORT_UNITS]) == expected
        # Nothing drawn: zero polygons. A drawn set counts its features.
        counts = {"empty": 0, "two": 2}
        class Management:
            @staticmethod
            def GetCount(value):
                return [counts[value]]
        arcpy.management = Management
        params[module.P_DRAWN_AOI].value = "empty"
        assert module._has_drawn_aoi(params) is False
        params[module.P_DRAWN_AOI].value = "two"
        assert module._has_drawn_aoi(params) is True
        # The polygon layer and a drawn area together are refused; either one alone is fine.
        params[module.P_AOI].value = "some_layer"
        tool.updateMessages(params)
        assert params[module.P_DRAWN_AOI].hasError()
        params2 = tool.getParameterInfo()
        params2[module.P_DRAWN_AOI].value = "two"
        tool.updateMessages(params2)
        assert not params2[module.P_DRAWN_AOI].hasError()
        print("TOOLBOX_DRAWN_OK")
        """
    )
    completed = subprocess.run([sys.executable, "-c", script, str(ROOT)], capture_output=True, text=True, timeout=120)
    assert "TOOLBOX_DRAWN_OK" in completed.stdout, completed.stdout[-1500:] + completed.stderr[-2500:]


def test_help_text_covers_the_new_fields() -> None:
    from hlzcore.help_text import FIELD_HELP, guidance

    for name in ("drawn_aoi", "report_units"):
        assert name in FIELD_HELP and all(k in FIELD_HELP[name] for k in ("title", "what", "requires", "effect"))
    assert "both" in guidance("drawn_aoi", {"drawn_given": True, "aoi_given": True}).lower()
    assert "feet" in guidance("report_units", {"units_choice": "IMPERIAL"}).lower()
