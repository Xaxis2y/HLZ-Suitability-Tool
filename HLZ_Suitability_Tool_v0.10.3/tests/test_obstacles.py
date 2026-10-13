# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Unit tests for the v0.6.9 listed obstacles (towers, masts, wires). Needs only NumPy and SciPy."""

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

from hlzcore.approach import analyze_candidate_approaches  # noqa: E402
from hlzcore.models import SECTOR_BLOCKED, SECTOR_CLEAR, GridGeometry  # noqa: E402
from hlzcore.obstacles import (  # noqa: E402
    Obstacle,
    ObstacleError,
    ObstacleSet,
    parse_height,
    project_obstacles,
    read_csv_obstacles,
    read_dof,
    read_geojson_obstacles,
    read_obstacle_file,
)

DOF_TEXT = """\
 CURRENCY DATE = 09/29/2026
ORS    OAS         V CO ST CITY             LATDMS      LONDMS       TYPE               Q AGL   AMSL  L H V M FAA STUDY        A JDATE
------------------------------------------------------------------------------------------------------------------------------
01-000001 O US AL ABBEVILLE        31 35 47.00N 085 15 44.00W TOWER              1 00322 00545 R 3 D I 2007-AWP-1234-OE  A 2007033
01-000002 O US AL ABBEVILLE        31 36 10.50N 085 16 02.25W POLE               1 00045 00268 N 3 D I 2009-AWP-0002-OE  C 2009100
01-000003 U US AL ABBEVILLE        31 37 00.00N 085 17 00.00W T-L TWR            1 00110 00330 N 5 E I                   D 2011200
99-000004 O US AL FAR AWAY         41 00 00.00N 090 00 00.00W TOWER              1 00500 00800 R 3 D I                   A 2012012
"""


def _tmp(name: str, text: str) -> Path:
    folder = Path(tempfile.mkdtemp())
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_height_units_and_text() -> None:
    assert abs(parse_height("100 ft") - 30.48) < 1.0e-6
    assert parse_height("30") == 30.0
    assert parse_height("30 m", "Feet") == 30.0  # a unit in the text wins
    assert abs(parse_height(100, "Feet") - 30.48) < 1.0e-6
    assert abs(parse_height(100, "Meters", "agl_ft") - 30.48) < 1.0e-6  # field name says feet
    assert parse_height(None) is None and parse_height("tall") is None and parse_height(0) is None and parse_height(-5) is None


def test_read_faa_dof() -> None:
    result = read_dof(_tmp("DOF.DAT", DOF_TEXT))
    assert result.records == 4 and result.skipped == 1  # the dismantled tower (action D) is skipped
    assert len(result.obstacles) == 3
    tower = result.obstacles[0]
    assert tower.kind == "point" and abs(tower.agl_m - 322 * 0.3048) < 1.0e-6 and "TOWER" in tower.name
    lon, lat = tower.parts[0][0]
    assert abs(lat - (31 + 35 / 60 + 47 / 3600)) < 1.0e-9 and abs(lon + (85 + 15 / 60 + 44 / 3600)) < 1.0e-9
    # A box around the first two records keeps only those; the far one is counted as outside.
    box = read_dof(_tmp("DOF.DAT", DOF_TEXT), bbox=(-85.5, 31.0, -85.0, 32.0))
    assert len(box.obstacles) == 2 and box.outside == 1
    try:
        read_dof(_tmp("empty.dat", "nothing here\n"))
    except ObstacleError as error:
        assert "no DOF obstacle records" in str(error)
    else:
        raise AssertionError("a file without records must raise")


def test_read_csv_and_geojson_osm() -> None:
    csv_path = _tmp("towers.csv", "name,lat,lon,height_ft\nMast A,45.40,-75.70,300\nMast B,45.41,-75.71,\nbad,x,y,10\n")
    csv_result = read_csv_obstacles(csv_path)
    assert len(csv_result.obstacles) == 2 and csv_result.skipped == 1 and csv_result.assumed == 1
    assert abs(csv_result.obstacles[0].agl_m - 91.44) < 1.0e-6 and csv_result.obstacles[1].assumed
    assert csv_result.obstacles[1].agl_m == 30.0
    try:
        read_csv_obstacles(_tmp("nocoords.csv", "a,b\n1,2\n"))
    except ObstacleError as error:
        assert "latitude and longitude" in str(error)
    else:
        raise AssertionError("a CSV without coordinates must raise")
    osm = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"man_made": "mast", "height": "45"}, "geometry": {"type": "Point", "coordinates": [-75.7, 45.4]}},
        {"type": "Feature", "properties": {"power": "line", "height": "100 ft"}, "geometry": {"type": "LineString", "coordinates": [[-75.7, 45.4], [-75.69, 45.41]]}},
        {"type": "Feature", "properties": {"building": "yes", "building:levels": "4"}, "geometry": {"type": "Polygon", "coordinates": [[[-75.7, 45.4], [-75.699, 45.4], [-75.699, 45.401], [-75.7, 45.4]]]}},
        {"type": "Feature", "properties": {"man_made": "tower"}, "geometry": {"type": "Point", "coordinates": [-75.6, 45.4]}},
    ]}
    result = read_geojson_obstacles(_tmp("osm.geojson", json.dumps(osm)), default_height_m=25.0)
    kinds = [o.kind for o in result.obstacles]
    heights = [round(o.agl_m, 2) for o in result.obstacles]
    assert kinds == ["point", "line", "polygon", "point"]
    assert heights == [45.0, 30.48, 12.0, 25.0]  # tag, feet, 4 levels x 3 m, then the default
    assert result.assumed == 1 and result.obstacles[3].assumed
    assert read_obstacle_file(_tmp("osm2.json", json.dumps(osm))).records == 4
    try:
        read_obstacle_file(_tmp("layer.shp", "x"))
    except ObstacleError as error:
        assert "not a supported obstacle file" in str(error)
    else:
        raise AssertionError("a shapefile is not an obstacle file")
    try:
        read_geojson_obstacles(_tmp("osm3.geojson", json.dumps(osm)), height_field="nothing")
    except ObstacleError as error:
        assert "has no property" in str(error)
    else:
        raise AssertionError("an unknown height field must raise")


def test_project_obstacles_keeps_structure() -> None:
    items = [Obstacle("point", [[(1.0, 2.0)]], 10.0), Obstacle("line", [[(1.0, 2.0), (3.0, 4.0)], [(5.0, 6.0), (7.0, 8.0)]], 20.0, "wire")]
    moved = project_obstacles(items, lambda lons, lats: ([x + 100.0 for x in lons], [y + 1000.0 for y in lats]))
    assert moved[0].parts == [[(101.0, 1002.0)]] and moved[1].parts[1] == [(105.0, 1006.0), (107.0, 1008.0)]
    assert moved[1].name == "wire" and moved[1].agl_m == 20.0


def test_obstacle_set_raster_samples_and_digest() -> None:
    grid = GridGeometry(0.0, 100.0, 1.0, -1.0, 100, 100)
    obstacle_set = ObstacleSet(
        [Obstacle("point", [[(50.0, 50.0)]], 40.0, "Tower"), Obstacle("line", [[(10.0, 10.0), (90.0, 10.0)]], 20.0, "Wire"),
         Obstacle("polygon", [[(70.0, 70.0), (80.0, 70.0), (80.0, 80.0), (70.0, 80.0)]], 12.0, "Shed")],
        buffer_m=4.0,
    )
    raster = obstacle_set.height_raster(grid)
    assert raster[50, 50] == 40.0 and raster[50, 53] == 40.0 and raster[50, 56] == 0.0  # radius 4 m
    assert raster[90, 50] == 20.0 and raster[84, 50] == 0.0  # the wire is 8 m wide (4 m each side)
    assert raster[25, 75] == 12.0  # inside the polygon
    assert obstacle_set.digest() == ObstacleSet(obstacle_set.obstacles, 4.0).digest() != ObstacleSet(obstacle_set.obstacles, 5.0).digest()
    dtm = np.full((100, 100), 10.0, dtype=np.float32)
    samples = obstacle_set.vertical_samples(grid, dtm, np.ones((100, 100), dtype=bool))
    assert samples is not None and float(samples.top_z.max()) == 50.0  # 10 m ground + 40 m tower
    distance, text = samples.nearest(50.0, 62.0, 500.0)
    assert abs(distance - 8.0) < 1.5 and "Tower" in text and "40 m AGL" in text  # 12 m to the tower, less the 4 m buffer
    assert samples.nearest(50.0, 62.0, 5.0) is None
    assert ObstacleSet([], 10.0).height_raster(grid).max() == 0.0 and ObstacleSet([], 10.0).vertical_samples(grid, dtm, np.ones((100, 100), bool)) is None


def test_exact_test_catches_a_thin_tower_between_two_rays() -> None:
    """A tower 120 m away, 2.5 degrees off the nearest ray: the 5 degree rays miss it, the exact test does not."""
    from test_core import MISSION, SMALL, _candidate

    aircraft = replace(SMALL, approach_range_m=200.0)
    size = 400
    grid = GridGeometry(0.0, float(size), 1.0, -1.0, size, size)
    flat = np.zeros((size, size), dtype=np.float32)
    candidate = _candidate(x=200.0, y=200.0, elevation_m=0.0)
    bearing = math.radians(92.5)  # between the rays at 90 and 95 degrees
    tower = Obstacle("point", [[(200.0 + 120.0 * math.sin(bearing), 200.0 + 120.0 * math.cos(bearing))]], 40.0, "Test tower")
    obstacle_set = ObstacleSet([tower], buffer_m=0.0)
    vertical = obstacle_set.vertical_samples(grid, flat, np.ones(flat.shape, dtype=bool))
    plain = analyze_candidate_approaches(candidate, flat, grid, aircraft, MISSION)
    listed = analyze_candidate_approaches(candidate, flat, grid, aircraft, MISSION, vertical)
    east_plain = next(s for s in plain if s.azimuth_deg == 90.0)
    east_listed = next(s for s in listed if s.azimuth_deg == 90.0)
    assert east_plain.classification == SECTOR_CLEAR and east_plain.worst_obstacle == ""
    assert east_listed.classification == SECTOR_BLOCKED and "Test tower" in east_listed.worst_obstacle
    assert 17.0 < east_listed.worst_angle_deg < 19.0  # atan((40 - 2) / 120)
    assert abs(east_listed.worst_distance_m - 120.0) < 0.5 and abs(east_listed.worst_height_above_lz_m - 40.0) < 1.0e-6
    # Sectors that do not contain the tower are unchanged.
    west_listed = next(s for s in listed if s.azimuth_deg == 270.0)
    assert west_listed.classification == SECTOR_CLEAR


def _flat_scene(size: int = 500):  # type: ignore[no-untyped-def]
    from hlzcore.pipeline import InputData

    valid = np.ones((size, size), dtype=bool)
    flat = np.zeros((size, size), dtype=np.float32)
    geometry = GridGeometry(0.0, float(size), 1.0, -1.0, size, size)
    return InputData(dtm=flat.copy(), dtm_valid=valid, geometry=geometry, dsm=flat.copy(), dsm_valid=valid.copy())


def test_tower_turns_a_green_approach_red_in_the_pipeline() -> None:
    from test_core import MISSION, SMALL, _NullWriter
    from hlzcore.pipeline import ArrayTileReader, RunOptions, run_tiled_pipeline
    from test_core import APPROVED

    aircraft = replace(SMALL, approach_range_m=150.0)
    mission = replace(MISSION, max_input_cells=10_000_000, max_candidates=5000, max_candidates_per_tile=500)

    def run(data):  # type: ignore[no-untyped-def]
        return run_tiled_pipeline(ArrayTileReader(data), aircraft, mission, APPROVED, RunOptions(), Path(tempfile.mkdtemp()), _NullWriter())

    base = run(_flat_scene())
    assert base.candidates and all(c.nearest_obstacle_m is None for c in base.candidates)
    assert all(c.clear_sector_count == c.assessed_sector_count for c in base.candidates if c.assessed_sector_count)  # flat: all clear

    data = _flat_scene()
    # A 60 m mast that the DSM does not show (the DSM is flat), next to the row of best candidates.
    data.obstacles = ObstacleSet([Obstacle("point", [[(180.0, 380.0)]], 60.0, "Mast")], buffer_m=5.0)
    listed = run(data)
    assert listed.metadata["obstacles"]["count"] == 1 and listed.metadata["inputs"]["obstacles"] != "none"
    near = [c for c in listed.candidates if 40.0 <= math.hypot(c.x - 180.0, c.y - 380.0) <= 120.0]
    assert near, "the scene must keep candidates near the mast"
    for candidate in near:
        bearing = math.degrees(math.atan2(180.0 - candidate.x, 380.0 - candidate.y)) % 360.0
        sector = min(candidate.sectors, key=lambda s: abs((s.azimuth_deg - bearing + 180.0) % 360.0 - 180.0))
        assert sector.classification == SECTOR_BLOCKED and "Mast" in sector.worst_obstacle, (candidate.candidate_id, bearing)
        assert candidate.nearest_obstacle_m is not None and "Mast" in candidate.nearest_obstacle_text
    # No touchdown point may sit on or next to the mast (the footprint is taller than the ring limit).
    for candidate in listed.candidates:
        assert math.hypot(candidate.x - 180.0, candidate.y - 380.0) > aircraft.analysis_radius_m + 5.0
    assert listed.metadata["results"]["cells_removed_by_obstacles"] > 0
    # Without a DSM the mast still blocks the approach.
    no_dsm = _flat_scene()
    no_dsm.dsm = None
    no_dsm.dsm_valid = None
    no_dsm.obstacles = data.obstacles
    result = run(no_dsm)
    assert any(s.classification == SECTOR_BLOCKED and "Mast" in s.worst_obstacle for c in result.candidates for s in c.sectors)
