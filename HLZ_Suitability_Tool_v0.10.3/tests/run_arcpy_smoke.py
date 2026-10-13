# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""ArcGIS Pro smoke test for the v0.10.3 toolbox (needs real ArcPy).

Run from the ArcGIS Pro Python Command Prompt inside a CLONED environment:
    conda create --clone arcgispro-py3 --name hlz-arcpy-v04 --pinned
    activate hlz-arcpy-v04
    python tests\\run_arcpy_smoke.py --log logs\\hlz_arcpy_smoke_v0.10.3.log

Scenarios (each logged step by step):
  A  DTM + aligned DSM + AOI polygon        -> full pipeline, styled outputs
  B  DTM only                                -> terrain-only mode, Marginal cap
  C  DTM + 2 m DSM (different grid)          -> automatic DSM resampling path
  D-G tiling, whole DTM, lake exclusion, point-and-radius search
  H  HLZ data folder (v0.6.6): tool 0 creates and audits it, then the analysis
     runs with ONLY the data folder given (DTM, DSM, water and AOI filled in)
  I  a river LINE as an exclusion layer, widened to 20 m (v0.6.7)
Return the log file for review.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import platform
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

LOG: list[str] = []


def log(message: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} | {message}"
    LOG.append(line)
    print(line, flush=True)


def check(condition: bool, label: str) -> bool:
    log(("PASS " if condition else "FAIL ") + label)
    return bool(condition)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default=str(ROOT / "logs" / "hlz_arcpy_smoke_v0.10.3.log"))
    parser.add_argument("--keep", action="store_true", help="keep the temporary workspace")
    args = parser.parse_args()
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    results: list[bool] = []
    workspace = Path(tempfile.mkdtemp(prefix="hlz_smoke_v067_"))
    try:
        log("HLZ Suitability Tool v0.10.3 - ArcPy smoke test")
        log(f"python {sys.version.split()[0]} | {sys.executable}")
        log(f"platform {platform.platform()} | env {Path(sys.prefix).name}")
        if Path(sys.prefix).name.lower() == "arcgispro-py3":
            log("WARNING running in arcgispro-py3 itself; please use a cloned environment")
        import numpy as np

        import arcpy

        info = arcpy.GetInstallInfo()
        log(f"ArcGIS {info.get('ProductName')} {info.get('Version')} | license {arcpy.ProductInfo()}")
        import scipy

        log(f"numpy {np.__version__} | scipy {scipy.__version__}")
        arcpy.env.overwriteOutput = True

        from synthetic import build_scene

        log(f"workspace {workspace}")
        log("Synthetic scene is 2.4 x 2.4 km; scenarios D/E use 300 k-cell tiles to force tiling")
        arcpy.management.CreateFileGDB(str(workspace), "inputs.gdb")
        gdb = str(workspace / "inputs.gdb")
        scene = build_scene()
        hole_cells = int(np.isnan(scene["dtm"]).sum())
        expected_whole = scene["dtm"].size - hole_cells
        aoi_block = scene["dtm"][900:1500, 900:1500]
        expected_aoi = int(np.isfinite(aoi_block).sum())
        log(f"synthetic NoData hole: {hole_cells} cells -> expected valid cells: whole DTM "
            f"{expected_whole:,}, AOI {expected_aoi:,}")
        spatial_reference = arcpy.SpatialReference(int(scene["epsg"]))
        size = scene["dtm"].shape[0]
        lower_left = arcpy.Point(scene["x_origin"], scene["y_origin"] - size * scene["cell"])
        for name in ("dtm", "dsm"):
            array = np.where(np.isfinite(scene[name]), scene[name], -9999.0).astype(np.float32)
            raster = arcpy.NumPyArrayToRaster(array, lower_left, scene["cell"], scene["cell"], -9999.0)
            raster_path = os.path.join(gdb, name)
            raster.save(raster_path)
            arcpy.management.DefineProjection(raster_path, spatial_reference)
            saved = arcpy.Raster(raster_path)
            log(f"created {name}: {saved.width} x {saved.height} cells")
        arcpy.management.Resample(f"{gdb}\\dsm", f"{gdb}\\dsm_2m", "2 2", "BILINEAR")
        log("created dsm_2m (2 m cells) for the resampling scenario")

        aoi = f"{gdb}\\aoi"
        arcpy.management.CreateFeatureclass(gdb, "aoi", "POLYGON", spatial_reference=spatial_reference)
        x0 = scene["x_origin"] + 900.0
        y1 = scene["y_origin"] - 900.0
        ring = arcpy.Array(
            [arcpy.Point(x0, y1), arcpy.Point(x0 + 600, y1), arcpy.Point(x0 + 600, y1 - 600), arcpy.Point(x0, y1 - 600)]
        )
        with arcpy.da.InsertCursor(aoi, ["SHAPE@"]) as cursor:
            cursor.insertRow([arcpy.Polygon(ring, spatial_reference)])
        log("created AOI polygon (central 600 x 600 m)")
        lake = os.path.join(gdb, "lake")
        arcpy.management.CreateFeatureclass(gdb, "lake", "POLYGON", spatial_reference=spatial_reference)
        lake_x, lake_y = x0 + 300.0, y1 - 300.0
        lake_ring = arcpy.Array([arcpy.Point(lake_x - 120, lake_y - 90), arcpy.Point(lake_x + 120, lake_y - 90),
                                 arcpy.Point(lake_x + 120, lake_y + 90), arcpy.Point(lake_x - 120, lake_y + 90)])
        with arcpy.da.InsertCursor(lake, ["SHAPE@"]) as cursor:
            cursor.insertRow([arcpy.Polygon(lake_ring, spatial_reference)])
        log("created a 240 x 180 m 'lake' polygon in the middle of the AOI (exclusion test)")

        pyt = ROOT / "toolbox" / "HLZ_Suitability.pyt"
        arcpy.ImportToolbox(str(pyt))
        log(f"imported toolbox {pyt}")
        from hlzcore.config import list_profiles, profiles_root

        medium = next(label for label in list_profiles("aircraft") if "generic_medium" in label)
        out_folder = workspace / "outputs"
        out_folder.mkdir()

        # Small tile budget (300 k cells per tile) forces many tiles: proves tiling,
        # seam handling and that there is no total-area limit (scenario E runs the
        # whole 5.76 M-cell DTM with no AOI).
        small_budget = workspace / "mission_small_tiles.json"
        mission_data = json.loads((profiles_root() / "missions" / "screening.json").read_text(encoding="utf-8"))
        mission_data.update(profile_id="smoke-small-tiles", max_input_cells=300_000)
        small_budget.write_text(json.dumps(mission_data, indent=2), encoding="utf-8")
        default_mission = str(profiles_root() / "missions" / "screening.json")

        scenarios = [
            ("A_dsm_aoi", f"{gdb}\\dtm", f"{gdb}\\dsm", aoi, default_mission, True),
            ("B_dtm_only", f"{gdb}\\dtm", None, aoi, default_mission, True),
            ("C_dsm_2m", f"{gdb}\\dtm", f"{gdb}\\dsm_2m", aoi, default_mission, True),
            ("D_many_tiles_aoi", f"{gdb}\\dtm", f"{gdb}\\dsm", aoi, str(small_budget), True),
            ("E_many_tiles_whole_dtm", f"{gdb}\\dtm", f"{gdb}\\dsm", None, str(small_budget), True),
            ("F_lake_excluded", f"{gdb}\\dtm", f"{gdb}\\dsm", aoi, default_mission, True),
            ("G_point_and_radius", f"{gdb}\\dtm", f"{gdb}\\dsm", None, str(small_budget), True),
        ]
        for name, dtm, dsm, aoi_layer, mission_file, should_succeed in scenarios:
            exclusion = lake if name.startswith("F_") else ""
            centre_text, radius = "", ""
            if name.startswith("G_"):
                ll = arcpy.PointGeometry(arcpy.Point(x0 + 300.0, y1 - 300.0), spatial_reference).projectAs(arcpy.SpatialReference(4326)).firstPoint
                centre_text, radius = f"{ll.Y:.6f}, {ll.X:.6f}", 150.0
            log(f"--- scenario {name} ---")
            started = time.perf_counter()
            try:
                result = arcpy.AnalyzeHLZ_hlz(
                    "",
                    dtm,
                    dsm or "",
                    aoi_layer or "",
                    "",
                    centre_text,
                    radius,
                    False,
                    exclusion,
                    "Meters",
                    medium,
                    "",
                    22.0,
                    29.92,
                    "OGE",
                    str(out_folder),
                    name,
                    False,
                    False,
                    mission_file,
                    str(profiles_root() / "doctrine" / "generic_planning.json"),
                    "",
                )
                for index in range(result.messageCount):
                    log("  GP> " + result.getMessage(index))
                log(f"scenario {name} finished in {time.perf_counter() - started:.1f} s")
            except Exception:  # noqa: BLE001
                messages_text = arcpy.GetMessages()
                log("  GP messages:\n" + messages_text)
                if not should_succeed:
                    results.append(
                        check("can be up to about" in messages_text,
                              f"{name}: refused with a size hint, as expected")
                    )
                    continue
                log(traceback.format_exc())
                results.append(check(False, f"{name}: tool ran without error"))
                continue
            if not should_succeed:
                results.append(check(False, f"{name}: should have been refused (budget)"))
                continue
            results.append(check(True, f"{name}: tool ran without error"))
            run = out_folder / name
            out_gdb = run / "HLZ_Results.gdb"
            for item in ("HLZ_Footprints", "HLZ_Best_Approach", "HLZ_Approach_Sectors", "Ground_Class", "Slope_deg"):
                results.append(check(arcpy.Exists(str(out_gdb / item)), f"{name}: {item} exists"))
            count = int(arcpy.management.GetCount(str(out_gdb / "HLZ_Footprints"))[0])
            results.append(check(count > 0, f"{name}: footprints count = {count}"))
            if exclusion:
                with arcpy.da.SearchCursor(lake, ["SHAPE@"]) as cursor:
                    lake_shape = next(iter(cursor))[0]
                with arcpy.da.SearchCursor(str(out_gdb / "HLZ_Footprints"), ["SHAPE@XY"]) as cursor:
                    centres = [row[0] for row in cursor]
                nearest = min(lake_shape.distanceTo(arcpy.PointGeometry(arcpy.Point(cx, cy), spatial_reference)) for cx, cy in centres)
                results.append(check(nearest >= 24.0, f"{name}: no touchdown point touches the lake (nearest centre {nearest:.1f} m, needs 25 m)"))
            if name.startswith("G_"):
                with arcpy.da.SearchCursor(str(out_gdb / "HLZ_Footprints"), ["SHAPE@XY", "Dist_Ctr_m"]) as cursor:
                    centre_rows = list(cursor)
                far = max(math.hypot(xy[0] - (x0 + 300.0), xy[1] - (y1 - 300.0)) for xy, _ in centre_rows)
                results.append(check(far <= 152.0, f"{name}: every candidate lies within the 150 m search radius (farthest {far:.1f} m)"))
                results.append(check(all(d is not None for _, d in centre_rows), f"{name}: the distance from the centre is stored"))
                results.append(check(arcpy.Exists(str(out_gdb / "HLZ_Search_Area")), f"{name}: the HLZ_Search_Area layer is written"))
            fields = [field.name for field in arcpy.ListFields(str(out_gdb / "HLZ_Footprints"))]
            log(f"  fields: {fields}")
            results.append(check({"Rank", "HLZ_ID", "Rating", "MGRS"}.issubset(fields), f"{name}: key fields present"))
            with arcpy.da.SearchCursor(
                str(out_gdb / "HLZ_Footprints"), ["Rank", "HLZ_ID", "Rating", "Score", "MGRS", "Latitude", "Longitude"],
                sql_clause=(None, "ORDER BY Rank"),
            ) as cursor:
                rows = list(cursor)
            for row in rows[:5]:
                log(f"  top: {row}")
            ratings = {row[2] for row in rows}
            log(f"  ratings present: {sorted(ratings)}")
            results.append(check(all(row[4] for row in rows), f"{name}: every candidate has an MGRS"))
            results.append(
                check(all(44.0 < (row[5] or 0) < 47.0 and -77.0 < (row[6] or 0) < -74.0 for row in rows),
                      f"{name}: lat/long near Ottawa")
            )
            if name == "B_dtm_only":
                results.append(
                    check(not ratings & {"Highly suitable", "Suitable"}, f"{name}: no-DSM cap at Marginal")
                )
            with (run / "HLZ_Candidates.csv").open(encoding="utf-8-sig") as stream:
                header = next(csv.reader(stream))
            results.append(check(header[0] == "Rank", f"{name}: CSV header first"))
            results.append(check((run / "HLZ_Report.html").stat().st_size > 5000, f"{name}: HTML report written"))
            metadata = json.loads((run / "run_metadata.json").read_text(encoding="utf-8"))
            valid_cells = metadata["results"]["valid_cell_count"]
            expected = valid_cells if name.startswith("G_") else (expected_aoi if aoi_layer else expected_whole)
            results.append(
                check(valid_cells == expected,
                      f"{name}: NoData hole masked (valid cells {valid_cells:,}, expected {expected:,})")
            )
            tiles = metadata["tiling"]["tile_count"]
            log(f"  tiles: {tiles} | seam duplicates removed: {metadata['results']['seam_duplicates_removed']}")
            if name.startswith(("D_", "E_")):
                results.append(check(tiles > 1, f"{name}: processed in {tiles} tiles"))
            ground = arcpy.Raster(str(out_gdb / "Ground_Class"))
            log(f"  Ground_Class raster {ground.width} x {ground.height}")
            if aoi_layer or name.startswith("G_"):
                results.append(check(ground.width <= 601 and ground.height <= 601,
                                     f"{name}: Ground_Class covers the AOI only"))
            else:
                results.append(check(ground.width == 2400 and ground.height == 2400,
                                     f"{name}: Ground_Class mosaicked over the whole DTM"))
            if name == "C_dsm_2m":
                results.append(
                    check(any("resampled" in item for item in metadata["warnings"]),
                          f"{name}: DSM resampling warning reported")
                )
            log_text = (run / "hlz_run.log").read_text(encoding="utf-8")
            for line in log_text.splitlines():
                if "WARNING" in line:
                    log("  run-log> " + line)

        # Scenario H: the data folder ------------------------------------------------
        log("--- scenario H_data_folder ---")
        try:
            from hlzcore import data_root as hlz_data

            data_root = workspace / "data_parent" / "HLZ_DATA"
            data_root.parent.mkdir()
            started = time.perf_counter()
            result = arcpy.PrepareDataFolder_hlz(str(data_root.parent), "HLZ_DATA", True, medium, False)
            for index in range(result.messageCount):
                log("  GP> " + result.getMessage(index))
            results.append(check(hlz_data.is_data_root(data_root), "H: tool 0 created the data folder and its marker"))
            results.append(check("NOT READY" in arcpy.GetMessages(), "H: an empty data folder is reported NOT READY"))
            arcpy.management.CopyRaster(f"{gdb}\\dtm", str(hlz_data.folder_path(data_root, "dtm") / "smoke_dtm.tif"),
                                        pixel_type="32_BIT_FLOAT", nodata_value="-9999", format="TIFF")
            arcpy.management.CopyRaster(f"{gdb}\\dsm", str(hlz_data.folder_path(data_root, "dsm") / "smoke_dsm.tif"),
                                        pixel_type="32_BIT_FLOAT", nodata_value="-9999", format="TIFF")
            arcpy.conversion.FeatureClassToShapefile([lake], str(hlz_data.folder_path(data_root, "hydrology")))
            arcpy.management.CopyFeatures(aoi, str(hlz_data.folder_path(data_root, "aoi") / "smoke_aoi.shp"))
            audit = hlz_data.audit_data_root(data_root, aircraft_profile=list_profiles("aircraft")[medium])
            for item in audit.findings:
                if item.level in ("FAIL", "WARN"):
                    log(f"  audit> [{item.level}] {item.message}")
            results.append(check(audit.status != hlz_data.STATUS_NOT_READY, f"H: pure-Python audit status {audit.status}"))
            results.append(check(bool(audit.inputs.dtm and audit.inputs.dsm and audit.inputs.exclusions and audit.inputs.aoi),
                                 "H: DTM, DSM, water and AOI found in the data folder"))
            dtm_found = next(d for d in audit.datasets if d.path == audit.inputs.dtm)
            real = arcpy.Raster(audit.inputs.dtm)
            results.append(check(dtm_found.width == real.width and abs(dtm_found.cell_w - real.meanCellWidth) < 1e-6
                                 and abs(dtm_found.extent[0] - real.extent.XMin) < 0.01
                                 and abs(dtm_found.extent[3] - real.extent.YMax) < 0.01,
                                 "H: GeoTIFF header reader agrees with ArcGIS (size, cell, extent)"))
            results.append(check(dtm_found.crs.epsg == int(scene["epsg"]),
                                 f"H: GeoTIFF CRS read as EPSG:{dtm_found.crs.epsg}"))
            result = arcpy.PrepareDataFolder_hlz(str(data_root.parent), "HLZ_DATA", True, medium, False)
            results.append(check((data_root / "09_Audit" / "HLZ_Data_Audit_latest.html").is_file(),
                                 "H: tool 0 wrote the audit report"))
            result = arcpy.AnalyzeHLZ_hlz(
                str(data_root), "", "", "", "", "", "", False, "", "Meters", medium, "", 22.0, 29.92, "OGE",
                str(hlz_data.folder_path(data_root, "runs")), "H_data_folder", False, False, default_mission,
                str(profiles_root() / "doctrine" / "generic_planning.json"), "",
            )
            for index in range(result.messageCount):
                log("  GP> " + result.getMessage(index))
            run_h = hlz_data.folder_path(data_root, "runs") / "H_data_folder"
            results.append(check((run_h / "HLZ_Report.html").is_file(), "H: analysis ran from the data folder alone"))
            metadata_h = json.loads((run_h / "run_metadata.json").read_text(encoding="utf-8"))
            text_h = json.dumps(metadata_h)
            results.append(check("smoke_dsm" in text_h and "lake" in text_h,
                                 "H: the DSM and the water layer from the data folder were used"))
            log(f"scenario H finished in {time.perf_counter() - started:.1f} s")
        except Exception:  # noqa: BLE001
            log("  GP messages:\n" + arcpy.GetMessages())
            log(traceback.format_exc())
            results.append(check(False, "H: data folder scenario ran without error"))

        # Scenario I: a river LINE as an exclusion layer ------------------------------------------
        log("--- scenario I_river_line ---")
        try:
            river = os.path.join(gdb, "river")
            arcpy.management.CreateFeatureclass(gdb, "river", "POLYLINE", spatial_reference=spatial_reference)
            river_y = y1 - 450.0
            with arcpy.da.InsertCursor(river, ["SHAPE@"]) as cursor:
                cursor.insertRow([arcpy.Polyline(arcpy.Array([arcpy.Point(x0 - 100.0, river_y), arcpy.Point(x0 + 700.0, river_y)]),
                                                 spatial_reference)])
            result = arcpy.AnalyzeHLZ_hlz(
                "", f"{gdb}\\dtm", f"{gdb}\\dsm", aoi, "", "", "", False, river, "Meters", medium, "", 22.0, 29.92,
                "OGE", str(out_folder), "I_river_line", False, False, default_mission,
                str(profiles_root() / "doctrine" / "generic_planning.json"), "", line_width_m=20.0,
            )
            for index in range(result.messageCount):
                log("  GP> " + result.getMessage(index))
            out_gdb_i = out_folder / "I_river_line" / "HLZ_Results.gdb"
            with arcpy.da.SearchCursor(str(out_gdb_i / "HLZ_Footprints"), ["SHAPE@XY"]) as cursor:
                nearest_river = min(abs(row[0][1] - river_y) for row in cursor)
            results.append(check(nearest_river >= 25.0 + 10.0 - 1.5,
                                 f"I: no touchdown point within 25 m + 10 m of the river line (nearest {nearest_river:.1f} m)"))
            meta_i = json.loads((out_folder / "I_river_line" / "run_metadata.json").read_text(encoding="utf-8"))
            results.append(check("lines widened to 20 m" in json.dumps(meta_i["inputs"]), "I: line width recorded in the metadata"))
        except Exception:  # noqa: BLE001
            log("  GP messages:\n" + arcpy.GetMessages())
            log(traceback.format_exc())
            results.append(check(False, "I: river line scenario ran without error"))

        # Scenario J: listed obstacles - a point layer with a height in feet, plus a CSV and a GeoJSON file (v0.6.9) ------
        log("--- scenario J_obstacles ---")
        try:
            tower_x, tower_y = x0 + 450.0, y1 - 300.0
            towers = os.path.join(gdb, "towers")
            arcpy.management.CreateFeatureclass(gdb, "towers", "POINT", spatial_reference=spatial_reference)
            arcpy.management.AddField(towers, "AGL_FT", "DOUBLE")
            arcpy.management.AddField(towers, "NAME", "TEXT", field_length=40)
            with arcpy.da.InsertCursor(towers, ["SHAPE@XY", "AGL_FT", "NAME"]) as cursor:
                cursor.insertRow([(tower_x, tower_y), 200.0, "Layer tower"])
            wgs = arcpy.SpatialReference(4326)
            moved = arcpy.PointGeometry(arcpy.Point(x0 + 150.0, y1 - 600.0), spatial_reference).projectAs(wgs).firstPoint
            csv_path = workspace / "towers_j.csv"
            csv_path.write_text(f"name,latitude,longitude,height_m\nCSV mast,{moved.Y:.7f},{moved.X:.7f},40\n", encoding="utf-8")
            geojson_path = workspace / "towers_j.geojson"
            geojson_path.write_text(json.dumps({"type": "FeatureCollection", "features": [{
                "type": "Feature", "properties": {"name": "No height"},
                "geometry": {"type": "Point", "coordinates": [moved.X + 0.001, moved.Y]}}]}), encoding="utf-8")
            result = arcpy.AnalyzeHLZ_hlz(
                "", f"{gdb}\\dtm", f"{gdb}\\dsm", aoi, "", "", "", False, "", "Meters", medium, "", 22.0, 29.92,
                "OGE", str(out_folder), "J_obstacles", False, False, default_mission,
                str(profiles_root() / "doctrine" / "generic_planning.json"), "",
                in_obstacle_layers=towers,
                in_obstacle_files=f"{csv_path};{geojson_path}",
                obstacle_height_field="", obstacle_height_units="Meters",
            )
            for index in range(result.messageCount):
                log("  GP> " + result.getMessage(index))
            run_j = out_folder / "J_obstacles"
            meta_j = json.loads((run_j / "run_metadata.json").read_text(encoding="utf-8"))
            obst_j = meta_j.get("obstacles") or {}
            results.append(check(obst_j.get("count") == 3, f"J: three obstacles applied (layer, CSV, GeoJSON): {obst_j.get('count')}"))
            results.append(check(obst_j.get("assumed_height_count") == 1, f"J: one obstacle has an assumed height: {obst_j.get('assumed_height_count')}"))
            heights = sorted(round(marker["agl"], 1) for marker in obst_j.get("markers", []))
            results.append(check(40.0 in heights and 61.0 in heights, f"J: heights read (layer feet converted to 61.0 m, CSV 40 m): {heights}"))
            with arcpy.da.SearchCursor(str(run_j / "HLZ_Results.gdb" / "HLZ_Footprints"), ["Obst_Dist_m"]) as cursor:
                distances = [row[0] for row in cursor if row[0] is not None]
            results.append(check(bool(distances) and min(distances) >= 35.0, f"J: no touchdown point within the cleared zone of an obstacle (nearest {min(distances) if distances else 'n/a'} m)"))
        except Exception:  # noqa: BLE001
            log("  GP messages:\n" + arcpy.GetMessages())
            log(traceback.format_exc())
            results.append(check(False, "J: obstacles scenario ran without error"))

        # Scenario K: point exclusions (known hazards) and the terrain overview grid (v0.7.0) ----------------------------
        log("--- scenario K_points_overview ---")
        try:
            hazards = os.path.join(gdb, "hazards")
            arcpy.management.CreateFeatureclass(gdb, "hazards", "POINT", spatial_reference=spatial_reference)
            hazard_xy = [(x0 + 100.0, y1 - 100.0), (x0 + 180.0, y1 - 180.0), (x0 + 90.0, y1 - 420.0)]
            with arcpy.da.InsertCursor(hazards, ["SHAPE@XY"]) as cursor:
                for xy in hazard_xy:
                    cursor.insertRow([xy])
            result = arcpy.AnalyzeHLZ_hlz(
                "", f"{gdb}\\dtm", f"{gdb}\\dsm", aoi, "", "", "", False, hazards, "Meters", medium, "", 22.0, 29.92,
                "OGE", str(out_folder), "K_points_overview", False, False, default_mission,
                str(profiles_root() / "doctrine" / "generic_planning.json"), "",
                point_radius_m=15.0, build_overview=True, overview_cell_m=100.0,
            )
            for index in range(result.messageCount):
                log("  GP> " + result.getMessage(index))
            run_k = out_folder / "K_points_overview"
            gdb_k = str(run_k / "HLZ_Results.gdb")
            meta_k = json.loads((run_k / "run_metadata.json").read_text(encoding="utf-8"))
            results.append(check("3 points" in json.dumps(meta_k["inputs"]) and "15 m radius" in json.dumps(meta_k["inputs"]),
                                 "K: three hazard points with a 15 m radius recorded in the metadata"))
            with arcpy.da.SearchCursor(f"{gdb_k}\\HLZ_Footprints", ["SHAPE@XY"]) as cursor:
                centres = [row[0] for row in cursor]
            nearest_hazard = min(math.hypot(cx - hx, cy - hy) for cx, cy in centres for hx, hy in hazard_xy)
            results.append(check(nearest_hazard >= 25.0 + 15.0 - 1.5,
                                 f"K: no touchdown point within 25 m + 15 m of a hazard point (nearest {nearest_hazard:.1f} m)"))
            results.append(check(arcpy.Exists(f"{gdb_k}\\HLZ_Overview_Grid"), "K: HLZ_Overview_Grid written to the geodatabase"))
            with arcpy.da.SearchCursor(f"{gdb_k}\\HLZ_Overview_Grid", ["Cand_Count", "Pass_Class", "Pct_Pass"]) as cursor:
                cells = list(cursor)
            results.append(check(len(cells) >= 4, f"K: {len(cells)} overview cells"))
            results.append(check(sum(row[0] for row in cells) == len(centres),
                                 f"K: the overview counts every candidate once ({sum(row[0] for row in cells)} of {len(centres)})"))
            results.append(check(any(row[1] in ("High", "Medium") for row in cells), "K: the open field shows as passing in the overview"))
            results.append(check((meta_k.get("overview") or {}).get("overview_cell_m") == 100.0, "K: the overview is recorded in the metadata"))
        except Exception:  # noqa: BLE001
            log("  GP messages:\n" + arcpy.GetMessages())
            log(traceback.format_exc())
            results.append(check(False, "K: points and overview scenario ran without error"))

        # Scenario L: area drawn on the map (a FeatureSet), report units, rejection summary, feet fields (v0.8.0) ----------
        log("--- scenario L_drawn_area ---")
        try:
            drawn_fc = os.path.join(gdb, "drawn_template")
            arcpy.management.CopyFeatures(aoi, drawn_fc)
            drawn = arcpy.FeatureSet(drawn_fc)
            result = arcpy.AnalyzeHLZ_hlz(
                "", f"{gdb}\\dtm", f"{gdb}\\dsm", "", "", "", "", False, "", "Meters", medium, "", 22.0, 29.92,
                "OGE", str(out_folder), "L_drawn_area", False, False, default_mission,
                str(profiles_root() / "doctrine" / "generic_planning.json"), "",
                drawn_aoi=drawn, report_units="Feet first (feet and metres)",
            )
            for index in range(result.messageCount):
                log("  GP> " + result.getMessage(index))
            run_l = out_folder / "L_drawn_area"
            gdb_l = str(run_l / "HLZ_Results.gdb")
            meta_l = json.loads((run_l / "run_metadata.json").read_text(encoding="utf-8"))
            results.append(check((meta_l["grid"]["width"], meta_l["grid"]["height"]) == (600, 600),
                                 f"L: the drawn polygon limited the analysis to the central 600 x 600 cells ({meta_l['grid']['width']} x {meta_l['grid']['height']})"))
            results.append(check(meta_l["options"]["report_units"] == "IMPERIAL", "L: report units recorded as IMPERIAL"))
            results.append(check((run_l / "HLZ_Rejection_Summary.csv").is_file(), "L: HLZ_Rejection_Summary.csv written"))
            fields_l = [field.name for field in arcpy.ListFields(f"{gdb_l}\\HLZ_Footprints")]
            results.append(check(all(name in fields_l for name in ("Elev_ft", "Rough_ft", "Obst_TDP_ft", "Dist_Ctr_ft")), "L: feet fields exist in HLZ_Footprints"))
            with arcpy.da.SearchCursor(f"{gdb_l}\\HLZ_Footprints", ["Elev_m", "Elev_ft"]) as cursor:
                pairs = [row for row in cursor if row[0] is not None]
            results.append(check(bool(pairs) and all(abs(ft - m / 0.3048) < 0.01 for m, ft in pairs), "L: Elev_ft agrees with Elev_m"))
            with arcpy.da.SearchCursor(f"{gdb_l}\\HLZ_Footprints", ["SHAPE@XY"]) as cursor:
                centres_l = [row[0] for row in cursor]
            results.append(check(bool(centres_l) and all(x0 - 1.0 <= cx <= x0 + 601.0 and y1 - 601.0 <= cy <= y1 + 1.0 for cx, cy in centres_l),
                                 f"L: every candidate lies inside the drawn polygon ({len(centres_l)} candidates)"))
            # A layer and a drawn area together must be refused.
            try:
                arcpy.AnalyzeHLZ_hlz(
                    "", f"{gdb}\\dtm", f"{gdb}\\dsm", aoi, "", "", "", False, "", "Meters", medium, "", 22.0, 29.92,
                    "OGE", str(out_folder), "L_both", False, False, default_mission,
                    str(profiles_root() / "doctrine" / "generic_planning.json"), "", drawn_aoi=drawn,
                )
                results.append(check(False, "L: a polygon layer together with a drawn area is refused"))
            except arcpy.ExecuteError:
                results.append(check(True, "L: a polygon layer together with a drawn area is refused"))
        except Exception:  # noqa: BLE001
            log("  GP messages:\n" + arcpy.GetMessages())
            log(traceback.format_exc())
            results.append(check(False, "L: drawn area scenario ran without error"))

        # Map styling cannot be tested outside an open project; record that.
        log("NOTE map styling (add_results_to_map) needs ArcGIS Pro UI - test manually with the tool dialog")
    except Exception:  # noqa: BLE001
        log(traceback.format_exc())
        results.append(False)
    finally:
        passed = sum(1 for item in results if item)
        failed = sum(1 for item in results if not item)
        log(f"SUMMARY passed={passed} failed={failed}")
        header = "# SPDX-License-Identifier: GPL-2.0-or-later\n# Copyright (c) 2026 Eui Soo SON\n"
        log_path.write_text(header + "\n".join(LOG) + "\n", encoding="utf-8")
        print(f"Log written to {log_path}")
        if not args.keep:
            shutil.rmtree(workspace, ignore_errors=True)
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
