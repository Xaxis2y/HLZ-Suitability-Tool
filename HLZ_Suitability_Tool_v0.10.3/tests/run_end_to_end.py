# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Command-line end-to-end test (needs rasterio; optional for toolbox users).

Run inside the dedicated CLI environment (never ``base``):
    conda env create -f environment.yml
    conda activate hlz-cli-v04
    python tests\\run_end_to_end.py --log logs\\hlz_end_to_end_v0.10.3.log
"""

from __future__ import annotations

import argparse
import csv
import json
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default=str(ROOT / "logs" / "hlz_end_to_end_v0.10.3.log"))
    args = parser.parse_args()
    results: list[tuple[bool, str]] = []
    workspace = Path(tempfile.mkdtemp(prefix="hlz_e2e_v067_"))

    def check(condition: bool, label: str) -> None:
        results.append((bool(condition), label))
        log(("PASS " if condition else "FAIL ") + label)

    try:
        from hlzcore.cli import main as cli_main
        from synthetic import X_ORIGIN, Y_ORIGIN, write_geotiffs

        log(f"python {sys.version.split()[0]} | env {Path(sys.prefix).name}")
        paths = write_geotiffs(workspace / "data")
        log(f"synthetic inputs: {paths}")
        aoi = [X_ORIGIN + 900, Y_ORIGIN - 1500, X_ORIGIN + 1500, Y_ORIGIN - 900]
        import math

        from rasterio.warp import transform

        lake_x, lake_y = X_ORIGIN + 900 + 150, Y_ORIGIN - 900 - 200
        steps = [2.0 * math.pi * i / 60 for i in range(60)]
        ring_xy = [(lake_x + 80 * math.cos(a), lake_y + 60 * math.sin(a)) for a in steps]
        lons, lats = transform("EPSG:32618", "EPSG:4326", [x for x, _ in ring_xy], [y for _, y in ring_xy])
        ring = [[lon, lat] for lon, lat in zip(lons, lats)]
        ring.append(ring[0])
        lake_file = workspace / "lake.geojson"
        lake_file.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
                             "geometry": {"type": "Polygon", "coordinates": [ring]}}]}), encoding="utf-8")
        river_y = Y_ORIGIN - 900 - 300
        river_xy = [(X_ORIGIN + 850, river_y), (X_ORIGIN + 1550, river_y)]
        r_lons, r_lats = transform("EPSG:32618", "EPSG:4326", [x for x, _ in river_xy], [y for _, y in river_xy])
        river_file = workspace / "river.geojson"
        river_file.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
                              "geometry": {"type": "LineString", "coordinates": [[a, b] for a, b in zip(r_lons, r_lats)]}}]}),
                              encoding="utf-8")
        from hlzcore.geo import latlon_to_mgrs

        centre_x, centre_y = X_ORIGIN + 900 + 300, Y_ORIGIN - 900 - 300
        (lon_c,), (lat_c,) = transform("EPSG:32618", "EPSG:4326", [centre_x], [centre_y])
        centre_lat, centre_lon = lat_c, lon_c
        centre_mgrs = latlon_to_mgrs(lat_c, lon_c)
        # v0.7.0: three known hazards (points) in the open field, plus the terrain overview grid.
        hazard_xy = [
            (X_ORIGIN + 900 + 120, Y_ORIGIN - 900 - 120),
            (X_ORIGIN + 900 + 200, Y_ORIGIN - 900 - 200),
            (X_ORIGIN + 900 + 80, Y_ORIGIN - 900 - 380),
        ]
        h_lons, h_lats = transform("EPSG:32618", "EPSG:4326", [x for x, _ in hazard_xy], [y for _, y in hazard_xy])
        hazard_file = workspace / "hazards.geojson"
        hazard_file.write_text(json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [lo, la]}}
            for lo, la in zip(h_lons, h_lats)]}), encoding="utf-8")
        scenarios = {
            "with_dsm": ["--dsm", str(paths["dsm"])],
            "with_hazards_overview": ["--dsm", str(paths["dsm"]), "--exclude", str(hazard_file), "--point-radius", "15",
                                      "--overview-cell", "100", "--units", "IMPERIAL"],
            "dtm_only": [],
            "with_lake": ["--dsm", str(paths["dsm"]), "--exclude", str(lake_file)],
            "with_river": ["--dsm", str(paths["dsm"]), "--exclude", str(river_file), "--line-width", "20"],
            "with_centre": ["--dsm", str(paths["dsm"]), "--centre", f"{centre_lat:.6f}, {centre_lon:.6f}", "--radius", "150"],
            "with_mgrs": ["--dsm", str(paths["dsm"]), "--centre", centre_mgrs, "--radius", "200"],
        }
        for name, extra in scenarios.items():
            log(f"--- scenario {name} ---")
            started = time.perf_counter()
            code = cli_main(
                [
                    "analyze",
                    "--dtm",
                    str(paths["dtm"]),
                    *extra,
                    "--output",
                    str(workspace / "out"),
                    "--run-name",
                    name,
                    "--aoi",
                    *[str(value) for value in aoi],
                    "--oat-c",
                    "22",
                ]
            )
            log(f"exit code {code} in {time.perf_counter() - started:.1f} s")
            check(code == 0, f"{name}: CLI exit code 0")
            run = workspace / "out" / name
            for item in (
                "Ground_Class.tif",
                "Slope_deg.tif",
                "Roughness_m.tif",
                "HLZ_Footprints.geojson",
                "HLZ_Best_Approach.geojson",
                "HLZ_Candidates.csv",
                "HLZ_Report.html",
                "run_metadata.json",
                "hlz_run.log",
            ):
                check((run / item).is_file(), f"{name}: {item} written")
            with (run / "HLZ_Candidates.csv").open(encoding="utf-8-sig") as stream:
                rows = list(csv.DictReader(stream))
            check(len(rows) > 0, f"{name}: {len(rows)} candidates")
            check(rows[0]["Rank"] == "1" and rows[0]["HLZ_ID"].startswith("HLZ-"), f"{name}: ranked IDs")
            check(all(row["MGRS"].startswith("18T") for row in rows), f"{name}: MGRS in zone 18T")
            ratings = sorted({row["Rating"] for row in rows})
            log(f"  ratings: {ratings}")
            for row in rows[:5]:
                log(
                    f"  {row['HLZ_ID']} {row['Rating']} score={row['Score_0_100']} slope={row['Slope_deg']} "
                    f"approach_from={row['Approach_from_deg']} angle={row['Approach_obstacle_angle_deg']} "
                    f"mgrs={row['MGRS']} limiting={row['Main_limiting_factor']}"
                )
            metadata = json.loads((run / "run_metadata.json").read_text(encoding="utf-8"))
            log(f"  results: {metadata['results']}")
            for warning in metadata["warnings"]:
                log(f"  warning: {warning}")
            # v0.8.0: rejection summary, feet columns, stage timing.
            check((run / "HLZ_Rejection_Summary.csv").is_file(), f"{name}: HLZ_Rejection_Summary.csv written")
            rej = metadata["rejections"]
            passed_cells = sum(item["cells"] for item in rej["reasons"] if item["passed"])
            check(passed_cells == metadata["results"]["ground_pass_cell_count"]
                  and rej["cells_evaluated"] <= metadata["results"]["valid_cell_count"]
                  and (metadata["search"] is not None or rej["cells_evaluated"] == metadata["results"]["valid_cell_count"]),
                  f"{name}: rejection summary agrees with the ground-pass and valid-cell counts (a search circle counts only its own cells)")
            check(rej["candidates"]["kept"] == len(rows), f"{name}: rejection summary lists the kept candidates ({len(rows)})")
            check("Elevation_ft" in rows[0] and abs(float(rows[0]["Elevation_ft"]) - float(rows[0]["Elevation_m"]) / 0.3048) < 0.01,
                  f"{name}: feet columns in the CSV agree with the metre columns")
            check("Why cells and sites were rejected" in (run / "HLZ_Report.html").read_text(encoding="utf-8"), f"{name}: report has the rejection section")
            check(metadata["stage_seconds"].get("tile_analysis", -1) >= 0 and any("Stage timing" in line for line in (run / "hlz_run.log").read_text(encoding="utf-8").splitlines()),
                  f"{name}: stage timing recorded")
            check(abs(sum(v for k, v in metadata["stage_seconds"].items() if k != "tile_read") - metadata["elapsed_seconds"]) < 5.0 + 0.2 * metadata["elapsed_seconds"],
                  f"{name}: stage seconds add up to about the elapsed time")
            if name in ("with_centre", "with_mgrs"):
                radius = 150.0 if name == "with_centre" else 200.0
                far = max(float(row["Distance_from_centre_m"]) for row in rows)
                check(far <= radius + 2.0, f"{name}: every candidate within {radius:.0f} m of the centre (farthest {far:.1f} m)")
                check(all(row["Bearing_from_centre_deg"] for row in rows), f"{name}: bearings are reported")
                check((run / "HLZ_Search_Area.geojson").is_file(), f"{name}: HLZ_Search_Area.geojson written")
                check(metadata["search"]["radius_m"] == radius, f"{name}: the search is recorded in the metadata")
                continue
            if name == "with_river":
                nearest = min(abs(float(row["Y_projected"]) - river_y) for row in rows)
                check(nearest >= 25.0 + 10.0 - 1.5, f"{name}: no touchdown point within 25 m + half the 20 m line width of the river line (nearest {nearest:.1f} m)")
                check(any(row["Distance_to_exclusion_m"] for row in rows), f"{name}: distances to the river are reported")
                meta_river = json.loads((run / "run_metadata.json").read_text(encoding="utf-8"))
                check("lines widened to 20 m" in json.dumps(meta_river["inputs"]), f"{name}: line width recorded in the metadata")
            if name == "with_lake":
                inside = [row for row in rows if ((float(row["X_projected"]) - lake_x) / 105.0) ** 2 + ((float(row["Y_projected"]) - lake_y) / 85.0) ** 2 < 1]
                check(not inside, f"{name}: no touchdown point on or beside the lake ({len(inside)} found)")
                check(any(row["Distance_to_exclusion_m"] for row in rows), f"{name}: distances to the lake are reported")
                check(not any("No exclusion layer" in w for w in metadata["warnings"]), f"{name}: no missing-layer warning")
            elif name == "with_river":
                check(not any("No exclusion layer" in w for w in metadata["warnings"]), f"{name}: no missing-layer warning")
            elif name == "with_hazards_overview":
                check(not any("No exclusion layer" in w for w in metadata["warnings"]), f"{name}: no missing-layer warning")
                closest = min(
                    math.hypot(float(row["X_projected"]) - hx, float(row["Y_projected"]) - hy)
                    for row in rows for hx, hy in hazard_xy
                )
                check(closest >= 25.0 + 15.0 - 1.5, f"{name}: no touchdown point within 25 m + the 15 m hazard radius of a point (nearest {closest:.1f} m)")
                meta_hazard = json.loads((run / "run_metadata.json").read_text(encoding="utf-8"))
                check("3 points with a 15 m radius" in json.dumps(meta_hazard["inputs"]), f"{name}: point radius recorded in the metadata")
                grid_path = run / "HLZ_Overview_Grid.geojson"
                check(grid_path.is_file(), f"{name}: HLZ_Overview_Grid.geojson written")
                grid_doc = json.loads(grid_path.read_text(encoding="utf-8"))
                features = grid_doc["features"]
                check(len(features) >= 4, f"{name}: {len(features)} overview cells")
                check(sum(item["properties"]["Cand_Count"] for item in features) == len(rows),
                      f"{name}: the overview counts every candidate exactly once")
                check(any(item["properties"]["Pass_Class"] in ("High", "Medium") for item in features),
                      f"{name}: the open field shows as passing in the overview")
                overview_meta = meta_hazard["overview"]
                check(overview_meta and overview_meta["overview_cell_m"] == 100.0 and overview_meta["cells"] == len(features),
                      f"{name}: the overview is recorded in the metadata")
                check(meta_hazard["options"]["report_units"] == "IMPERIAL" and " ft (" in (run / "HLZ_Report.html").read_text(encoding="utf-8"),
                      f"{name}: --units IMPERIAL writes feet first in the report")
                footprint_props = json.loads((run / "HLZ_Footprints.geojson").read_text(encoding="utf-8"))["features"][0]["properties"]
                check("Elevation_ft" in footprint_props and footprint_props["Elevation_ft"] is not None, f"{name}: GeoJSON footprints carry feet properties")
            else:
                check(any("No exclusion layer" in w for w in metadata["warnings"]), f"{name}: warns that no exclusion layer was given")
            if name == "dtm_only":
                check(not set(ratings) & {"Highly suitable", "Suitable"}, f"{name}: rating capped at Marginal")
            else:
                check("Highly suitable" in ratings, f"{name}: open field rated Highly suitable")
                # The ditch (feature rows 300-302) must not carry a touchdown point.
                ditch_y = Y_ORIGIN - 900 - 301
                check(
                    all(abs(float(row["Y_projected"]) - ditch_y) > 25.0 for row in rows if float(row["X_projected"]) < X_ORIGIN + 900 + 260),
                    f"{name}: no TDP centred on the ditch",
                )
                forest = [
                    row for row in rows
                    if X_ORIGIN + 900 + 270 <= float(row["X_projected"]) <= X_ORIGIN + 900 + 340
                    and Y_ORIGIN - 900 - 450 <= float(row["Y_projected"]) <= Y_ORIGIN - 900 - 150
                ]
                check(not forest, f"{name}: no candidate inside the forest block")
        # v0.6.9: listed obstacles (FAA DOF, CSV, GeoJSON) - a 60 m tower in the open field.
        tower_x, tower_y = X_ORIGIN + 900 + 450, Y_ORIGIN - 900 - 100
        (t_lon,), (t_lat,) = transform("EPSG:32618", "EPSG:4326", [tower_x], [tower_y])

        def dms(value: float, positive: str, negative: str, degree_digits: int) -> str:
            hemisphere = positive if value >= 0 else negative
            value = abs(value)
            degrees = int(value)
            minutes = int((value - degrees) * 60)
            seconds = (value - degrees - minutes / 60.0) * 3600.0
            return f"{degrees:0{degree_digits}d} {minutes:02d} {seconds:05.2f}{hemisphere}"

        tower_agl_m = 60.0
        dof_file = workspace / "DOF.DAT"
        dof_file.write_text(
            " CURRENCY DATE = 10/01/2026\n"
            "ORS    OAS         V CO ST CITY             LATDMS      LONDMS       TYPE               Q AGL   AMSL  L H V M FAA STUDY        A JDATE\n"
            "------------------------------------------------------------------------------------------------------------------------------\n"
            f"18-000001 O US NY TESTVILLE        {dms(t_lat, 'N', 'S', 2)} {dms(t_lon, 'E', 'W', 3)} TOWER              1 "
            f"{round(tower_agl_m / 0.3048):05d} 00545 R 3 D I 2007-AWP-1234-OE  A 2007033\n",
            encoding="utf-8",
        )
        csv_file = workspace / "towers.csv"
        csv_file.write_text(f"name,latitude,longitude,height_m\nTest mast,{t_lat:.7f},{t_lon:.7f},{tower_agl_m}\n", encoding="utf-8")
        geojson_file = workspace / "towers.geojson"
        geojson_file.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature",
                                "properties": {"name": "Test mast", "height": f"{tower_agl_m} m"},
                                "geometry": {"type": "Point", "coordinates": [t_lon, t_lat]}}]}), encoding="utf-8")
        for name, extra in {
            "obst_dof": ["--obstacles", str(dof_file)],
            "obst_csv": ["--obstacles", str(csv_file)],
            "obst_geojson": ["--obstacles", str(geojson_file)],
            "obst_nodsm": ["--obstacles", str(geojson_file), "--dtm-only"],
        }.items():
            log(f"--- scenario {name} ---")
            argv = ["analyze", "--dtm", str(paths["dtm"]), "--output", str(workspace / "out"), "--run-name", name,
                    "--aoi", *[str(value) for value in aoi], "--oat-c", "22", *extra]
            if "--dtm-only" in argv:
                argv.remove("--dtm-only")
            else:
                argv += ["--dsm", str(paths["dsm"])]
            code = cli_main(argv)
            check(code == 0, f"{name}: CLI exit code 0")
            run = workspace / "out" / name
            if not (run / "run_metadata.json").is_file():
                continue
            meta = json.loads((run / "run_metadata.json").read_text(encoding="utf-8"))
            obst = meta.get("obstacles") or {}
            check(obst.get("count") == 1, f"{name}: one obstacle applied ({obst.get('count')})")
            check(obst.get("markers") and abs(obst["markers"][0]["agl"] - tower_agl_m) < 0.6,
                  f"{name}: height {tower_agl_m} m read correctly ({obst.get('markers')})")
            with (run / "HLZ_Candidates.csv").open(encoding="utf-8-sig") as stream:
                rows = list(csv.DictReader(stream))
            check("Nearest_obstacle_m" in rows[0] and "Nearest_obstacle" in rows[0], f"{name}: obstacle columns in the CSV")
            near = [float(row["Nearest_obstacle_m"]) for row in rows if row["Nearest_obstacle_m"]]
            check(bool(near) and min(near) >= 35.0, f"{name}: no candidate within the tower's cleared zone (nearest {min(near) if near else 'n/a'} m)")
            report = (run / "HLZ_Report.html").read_text(encoding="utf-8")
            check("Purple dots" in report and "Obstacles:" in report, f"{name}: the report shows the obstacle")
            if name == "obst_nodsm":
                check(any("surface model" in w.lower() or "no dsm" in w.lower() for w in meta["warnings"]),
                      f"{name}: warns that the obstacles are applied without a surface model")
        # v0.6.6: the data folder. Create, audit (not ready), fill, audit (ready), analyse with --data-root only.
        log("--- scenario data_root ---")
        from hlzcore import data_root as hlz_data

        data = workspace / "HLZ_DATA"
        code = cli_main(["init-data", str(data), "--quiet"])
        check(code == 2 and hlz_data.is_data_root(data), f"data_root: init-data creates the folder, NOT READY (exit {code})")
        shutil.copy(paths["dtm"], hlz_data.folder_path(data, "dtm") / "e2e_dtm.tif")
        shutil.copy(paths["dsm"], hlz_data.folder_path(data, "dsm") / "e2e_dsm.tif")
        shutil.copy(lake_file, hlz_data.folder_path(data, "hydrology") / "lake.geojson")
        shutil.copy(river_file, hlz_data.folder_path(data, "exclusions") / "river.geojson")
        audit = hlz_data.audit_data_root(data)
        for item in audit.findings:
            if item.level in ("FAIL", "WARN"):
                log(f"  audit> [{item.level}] {item.message}")
        check(audit.status != hlz_data.STATUS_NOT_READY, f"data_root: audit status {audit.status}")
        check(audit.inputs.dtm.endswith("e2e_dtm.tif") and audit.inputs.dsm.endswith("e2e_dsm.tif"),
              "data_root: DTM and DSM found")
        check(len(audit.inputs.exclusions) == 2, "data_root: the lake polygons and the river lines are exclusion layers")
        check(any("river.geojson" in item.message and item.level == "INFO" and "LINES" in item.message for item in audit.findings),
              "data_root: the audit says river.geojson holds lines widened to the line width")
        check(any("lake.geojson" in item.message and item.level == "PASS" for item in audit.findings),
              "data_root: the WGS84 lake was compared with the UTM DTM and overlaps it")
        import rasterio

        with rasterio.open(paths["dtm"]) as source:
            found = next(d for d in audit.datasets if d.path == audit.inputs.dtm)
            bounds = source.bounds
            check((found.width, found.height) == (source.width, source.height)
                  and max(abs(a - b) for a, b in zip(found.extent, (bounds.left, bounds.bottom, bounds.right, bounds.top))) < 1e-6,
                  "data_root: GeoTIFF header reader agrees with GDAL")
        code = cli_main(["analyze", "--data-root", str(data), "--run-name", "from_data_root",
                         "--aoi", *[str(value) for value in aoi]])
        run = hlz_data.folder_path(data, "runs") / "from_data_root"
        check(code == 0 and (run / "HLZ_Report.html").is_file(), f"data_root: analyze --data-root ran (exit {code})")
        if (run / "run_metadata.json").is_file():
            meta = json.loads((run / "run_metadata.json").read_text(encoding="utf-8"))
            text = json.dumps(meta["inputs"])
            check("e2e_dsm.tif" in text and "lake.geojson" in text and "river.geojson" in text and "lines widened to 10 m" in text,
                  "data_root: DSM, lake and river line taken from the data folder (lines widened to the default 10 m)")
    except Exception:  # noqa: BLE001
        log(traceback.format_exc())
        results.append((False, "unexpected exception"))
    finally:
        passed = sum(1 for ok, _ in results if ok)
        failed = len(results) - passed
        log(f"SUMMARY passed={passed} failed={failed}")
        log_path = Path(args.log)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        header = "# SPDX-License-Identifier: GPL-2.0-or-later\n# Copyright (c) 2026 Eui Soo SON\n"
        log_path.write_text(header + "\n".join(LOG) + "\n", encoding="utf-8")
        print(f"Log written to {log_path}")
        shutil.rmtree(workspace, ignore_errors=True)
    return 0 if results and all(ok for ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
