# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Command-line interface (optional; the ArcGIS Pro toolbox does not need it).

Example (inside a dedicated Conda environment, never ``base``)::

    python -m hlzcore.cli analyze --dtm dtm.tif --dsm dsm.tif --output out_dir
    python -m hlzcore.cli init-data C:\\GIS\\HLZ_DATA
    python -m hlzcore.cli audit-data C:\\GIS\\HLZ_DATA
    python -m hlzcore.cli analyze --data-root C:\\GIS\\HLZ_DATA
    python -m hlzcore.cli list-profiles
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import (
    list_profiles,
    load_aircraft_profile,
    load_doctrine_profile,
    load_mission_profile,
    profiles_root,
)
from . import data_root as hlz_data


def build_parser() -> argparse.ArgumentParser:
    root = profiles_root()
    parser = argparse.ArgumentParser(prog="hlz", description="Helicopter landing-zone screening tool.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="Run an HLZ analysis")
    analyze.add_argument(
        "--data-root",
        help="HLZ data folder: --dtm, --dsm, --exclude (one GeoJSON) and --output default to its contents",
    )
    analyze.add_argument("--dtm", help="Bare-earth DTM (GeoTIFF, projected metres); required without --data-root")
    analyze.add_argument("--dsm", help="Surface model (DSM); resampled to the DTM grid if needed")
    analyze.add_argument("--output", help="Output parent folder (default with --data-root: its 08_Runs)")
    analyze.add_argument("--run-name", default=None, help="Run sub-folder name")
    analyze.add_argument("--z-units", choices=["METERS", "FEET"], default="METERS")
    analyze.add_argument(
        "--centre",
        action="append",
        help="Search around a point: MGRS (18T VR 45906 30941) or latitude, longitude (45.4236, -75.7009). Repeat for several points; needs --radius",
    )
    analyze.add_argument("--radius", type=float, help="Search radius in metres around each --centre")
    analyze.add_argument(
        "--exclude",
        action="append",
        help="GeoJSON polygons (lakes, wetlands), lines (rivers, streams, shorelines) and/or points (known hazards) that a touchdown point must not touch",
    )
    analyze.add_argument(
        "--line-width", type=float, default=10.0,
        help="Total width in metres given to LINES in --exclude (default 10). Wider rivers should be polygons",
    )
    analyze.add_argument(
        "--point-radius", type=float, default=10.0,
        help="Radius in metres given to POINTS in --exclude (default 10)",
    )
    analyze.add_argument(
        "--overview-cell", type=float, default=0.0,
        help="Write a terrain overview grid (HLZ_Overview_Grid.geojson) with cells of this size in metres, for example "
             "500 (default 0 = no overview; minimum 20, and at least 16 analysis cells)",
    )
    analyze.add_argument(
        "--obstacles", nargs="+",
        help="Obstacle files: FAA Digital Obstacle File (.dat), CSV (latitude, longitude, height columns) or GeoJSON "
             "(WGS84, OpenStreetMap exports work). Towers, masts, wires and buildings the lidar surface misses",
    )
    analyze.add_argument("--obstacle-height-field", help="Name of the height column / property (found automatically if omitted)")
    analyze.add_argument("--obstacle-units", default="Meters", choices=["Meters", "Feet"], help="Unit of the heights (default Meters; FAA DOF is always feet)")
    analyze.add_argument("--obstacle-default-height", type=float, default=30.0, help="Height in metres for obstacles without one (default 30)")
    analyze.add_argument("--obstacle-buffer", type=float, default=10.0, help="Horizontal buffer in metres around each obstacle (default 10)")
    analyze.add_argument("--exclude-crs", default="EPSG:4326", help="Coordinate system of the --exclude file")
    analyze.add_argument(
        "--aoi",
        nargs=4,
        type=float,
        metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
        help="AOI bounding box in the DTM CRS (no size limit; omit for the whole DTM)",
    )
    analyze.add_argument("--aircraft-profile", default=str(root / "aircraft" / "generic_medium.json"))
    analyze.add_argument("--mission-profile", default=str(root / "missions" / "screening.json"))
    analyze.add_argument("--doctrine-profile", default=str(root / "doctrine" / "generic_planning.json"))
    analyze.add_argument("--oat-c", type=float, help="Outside air temperature (C)")
    analyze.add_argument("--altimeter-inhg", type=float, default=29.92)
    analyze.add_argument("--performance-mode", choices=["IGE", "OGE"], default="OGE")
    analyze.add_argument("--max-candidates", type=int, default=None)
    analyze.add_argument(
        "--diagnostic-rasters",
        choices=["AUTO", "ALWAYS", "NEVER"],
        default="AUTO",
        help="Slope/roughness/obstacle rasters (AUTO skips them above 100 M cells)",
    )
    analyze.add_argument(
        "--basemap",
        choices=["ONLINE", "OFFLINE"],
        default="ONLINE",
        help="Map tiles in the HTML report: ONLINE (third-party servers) or OFFLINE (no network requests)",
    )
    analyze.add_argument("--verbose", action="store_true")

    validate = subparsers.add_parser("validate-profiles", help="Validate profiles")
    validate.add_argument("--aircraft-profile", required=True)
    validate.add_argument("--mission-profile", required=True)
    validate.add_argument("--doctrine-profile", required=True)

    subparsers.add_parser("list-profiles", help="List shipped profiles")

    init_data = subparsers.add_parser("init-data", help="Create an HLZ data folder (missing parts only) and audit it")
    init_data.add_argument("root", help="Data folder, e.g. C:\\GIS\\HLZ_DATA")
    audit_data = subparsers.add_parser("audit-data", help="Audit an HLZ data folder and write a report to 09_Audit")
    audit_data.add_argument("root", help="The data folder")
    for command in (init_data, audit_data):
        command.add_argument("--aircraft-profile", dest="audit_aircraft", help="Aircraft for the AOI margin check")
        command.add_argument("--quiet", action="store_true")
    return parser


def _apply_data_root(args: argparse.Namespace) -> None:
    """Fill --dtm, --dsm, --exclude and --output from a data folder when they were not given."""
    if not args.data_root:
        return
    found = hlz_data.discover_inputs(args.data_root)
    if not args.dtm:
        if not found.dtm:
            raise ValueError(
                "The data folder has no single DTM in 01_Elevation\\DTM. Run: python -m hlzcore.cli audit-data "
                f'"{args.data_root}"'
            )
        args.dtm = found.dtm
    if not args.dsm and found.dsm:
        args.dsm = found.dsm
    if not args.exclude:
        geojson = [path for path in found.exclusions if path.lower().endswith((".geojson", ".json"))]
        if geojson:
            args.exclude = geojson
        if len(geojson) < len(found.exclusions):
            print(
                f"NOTE: the command line reads GeoJSON exclusion files only; {len(found.exclusions) - len(geojson)} of "
                f"the data folder's {len(found.exclusions)} exclusion layer(s) are not GeoJSON and are NOT used. Convert "
                "them to GeoJSON, or use the ArcGIS Pro toolbox, which uses all of them."
            )
    if not args.obstacles and found.obstacles:
        usable = [path for path in found.obstacles if path.lower().endswith((".dat", ".csv", ".txt", ".geojson", ".json"))]
        if usable:
            args.obstacles = usable
        if len(usable) < len(found.obstacles):
            print(
                f"NOTE: the command line reads FAA DOF, CSV and GeoJSON obstacle files only; "
                f"{len(found.obstacles) - len(usable)} obstacle layer(s) in the data folder are NOT used. Convert "
                "them to GeoJSON, or use the ArcGIS Pro toolbox."
            )
    if not args.output and found.runs_folder:
        args.output = found.runs_folder
    print(f"Data folder {found.root}: DTM {args.dtm} | DSM {args.dsm or 'none'} | exclusion "
          f"{', '.join(args.exclude) if args.exclude else 'none'} | obstacles "
          f"{', '.join(args.obstacles) if args.obstacles else 'none'}")


def _close_logging_quietly() -> None:
    try:
        from .pipeline import close_logging

        close_logging()
    except Exception:  # noqa: BLE001 - NumPy/SciPy may be missing in a plain Python
        pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list-profiles":
            for kind in ("aircraft", "missions", "doctrine"):
                print(f"[{kind}]")
                for label, path in list_profiles(kind).items():
                    print(f"  {label}: {path}")
            return 0
        if args.command in ("init-data", "audit-data"):
            result = hlz_data.audit_data_root(
                args.root, create=args.command == "init-data", aircraft_profile=args.audit_aircraft
            )
            print(f"{result.status}: {result.root}" if args.quiet else hlz_data.format_audit_text(result))
            return 2 if result.status == hlz_data.STATUS_NOT_READY else 0
        if args.command == "validate-profiles":
            result = {
                "aircraft": load_aircraft_profile(args.aircraft_profile).profile_id,
                "mission": load_mission_profile(args.mission_profile).profile_id,
                "doctrine": load_doctrine_profile(args.doctrine_profile).profile_id,
            }
            print(json.dumps(result, indent=2))
            return 0

        from .io_rasterio import RasterioTileReader, RasterioWriter
        from .pipeline import RunOptions, close_logging, configure_logging, run_tiled_pipeline
        from .search import parse_search_centre
        from .tiling import checkpoint_status

        _apply_data_root(args)
        if not args.dtm or not args.output:
            raise ValueError("--dtm and --output are required (or give --data-root)")

        aircraft = load_aircraft_profile(args.aircraft_profile)
        mission = load_mission_profile(args.mission_profile)
        doctrine = load_doctrine_profile(args.doctrine_profile)
        run_name = args.run_name or f"HLZ_{aircraft.profile_id}_{datetime.now():%Y%m%d_%H%M%S}"
        output_dir = Path(args.output).resolve() / run_name
        resuming = checkpoint_status(output_dir) == "incomplete"
        configure_logging(output_dir / "hlz_run.log", args.verbose, append=resuming)
        search_latlon = None
        if args.centre or args.radius:
            if not (args.centre and args.radius and args.radius > 0):
                raise ValueError("Searching around a point needs both --centre and a --radius greater than zero")
            search_latlon = [parse_search_centre(text)[:2] for text in args.centre]
        reader = RasterioTileReader(
            args.dtm, args.dsm, args.z_units, tuple(args.aoi) if args.aoi else None, args.exclude, args.exclude_crs,
            search_latlon, args.radius, args.line_width, args.point_radius,
            obstacle_paths=args.obstacles,
            obstacle_height_field=args.obstacle_height_field,
            obstacle_height_units=args.obstacle_units,
            obstacle_default_height_m=args.obstacle_default_height,
            obstacle_buffer_m=args.obstacle_buffer,
        )
        writer = RasterioWriter(output_dir, reader.crs, reader.profile)
        try:
            result = run_tiled_pipeline(
                reader,
                aircraft,
                mission,
                doctrine,
                RunOptions(
                    run_name=run_name,
                    oat_c=args.oat_c,
                    altimeter_inhg=args.altimeter_inhg,
                    performance_mode=args.performance_mode,
                    max_candidates_override=args.max_candidates,
                    diagnostic_rasters=args.diagnostic_rasters,
                    report_basemap=args.basemap,
                    overview_cell_m=args.overview_cell or None,
                ),
                output_dir,
                writer,
                profile_sources={
                    "aircraft": str(Path(args.aircraft_profile).resolve()),
                    "mission": str(Path(args.mission_profile).resolve()),
                    "doctrine": str(Path(args.doctrine_profile).resolve()),
                },
            )
        finally:
            reader.close()
        print()
        print("\n".join(result.summary_lines))
        print()
        print(f"Report: {result.outputs['report']}")
        print("Re-running with the same --run-name resumes an interrupted run.")
        close_logging()
        return 0
    except Exception as error:  # noqa: BLE001 - CLI boundary
        print(f"HLZ {args.command} failed: {error}", file=sys.stderr)
        _close_logging_quietly()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
