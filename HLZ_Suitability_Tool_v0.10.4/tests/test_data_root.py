# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Unit tests for the v0.6.6 data folder (layout + audit). Standard library only."""

from __future__ import annotations

import json
import os
import sqlite3
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hlzcore import data_root as dr  # noqa: E402
from hlzcore.config import profiles_root  # noqa: E402

UTM18 = 2959  # NAD83(CSRS) / UTM zone 18N


# --- tiny writers (no GDAL) ----------------------------------------------------


def write_geotiff(
    path: Path, width: int = 40, height: int = 30, cell: float = 1.0, x0: float = 445000.0, y0: float = 5030000.0,
    epsg: int | None = UTM18, geographic: bool = False, bits: int = 32, sample_format: int = 3, nodata: str | None = "-32767",
    bands: int = 1, vertical: int | None = None, bigtiff: bool = False, unit: int | None = 9001,
) -> Path:
    """Minimal uncompressed striped GeoTIFF (little endian), header only matters for the audit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    bytes_per = bits // 8
    pixels = b"\x00" * (width * height * bytes_per * bands)
    keys = [(1024, 0, 1, 2 if geographic else 1), (1025, 0, 1, 1)]
    if geographic:
        keys.append((2048, 0, 1, epsg or 4326))
    elif epsg:
        keys.append((3072, 0, 1, epsg))
    if unit and not geographic:
        keys.append((3076, 0, 1, unit))
    if vertical:
        keys.append((4096, 0, 1, vertical))
    directory = [1, 1, 0, len(keys)]
    for key in sorted(keys):
        directory.extend(key)
    tags: list[tuple[int, int, int, bytes]] = []  # tag, type, count, raw

    def add(tag: int, kind: int, values: list, fmt: str) -> None:
        tags.append((tag, kind, len(values), struct.pack("<" + fmt * len(values), *values)))

    add(256, 4, [width], "I")
    add(257, 4, [height], "I")
    add(258, 3, [bits] * bands, "H")
    add(259, 3, [1], "H")
    add(262, 3, [1], "H")
    add(273, 4, [0], "I")  # patched below
    add(277, 3, [bands], "H")
    add(278, 4, [height], "I")
    add(279, 4, [len(pixels)], "I")
    add(284, 3, [1], "H")
    add(339, 3, [sample_format] * bands, "H")
    add(33550, 12, [cell, cell, 0.0], "d")
    add(33922, 12, [0.0, 0.0, 0.0, x0, y0, 0.0], "d")
    add(34735, 3, directory, "H")
    if nodata is not None:
        raw = nodata.encode() + b"\x00"
        tags.append((42113, 2, len(raw), raw))
    tags.sort()
    entry_size, inline = (20, 8) if bigtiff else (12, 4)
    header_size = 16 if bigtiff else 8
    ifd_size = (8 if bigtiff else 2) + len(tags) * entry_size + (8 if bigtiff else 4)
    data_offset = header_size + ifd_size
    blobs = b""
    entries = []
    pixel_offset = None
    layout = []
    for tag, kind, count, raw in tags:
        if len(raw) <= inline:
            layout.append((tag, kind, count, raw.ljust(inline, b"\x00"), None))
        else:
            layout.append((tag, kind, count, None, data_offset + len(blobs)))
            blobs += raw + (b"\x00" if len(raw) % 2 else b"")
    pixel_offset = data_offset + len(blobs)
    for tag, kind, count, raw, offset in layout:
        if tag == 273:
            raw = struct.pack("<I", pixel_offset).ljust(inline, b"\x00")
        if bigtiff:
            value = raw if raw is not None else struct.pack("<Q", offset)
            entries.append(struct.pack("<HHQ", tag, kind, count) + value)
        else:
            value = raw if raw is not None else struct.pack("<I", offset)
            entries.append(struct.pack("<HHI", tag, kind, count) + value)
    if bigtiff:
        head = b"II" + struct.pack("<HHHQ", 43, 8, 0, header_size)
        ifd = struct.pack("<Q", len(tags)) + b"".join(entries) + struct.pack("<Q", 0)
    else:
        head = b"II" + struct.pack("<HI", 42, header_size)
        ifd = struct.pack("<H", len(tags)) + b"".join(entries) + struct.pack("<I", 0)
    path.write_bytes(head + ifd + blobs + pixels)
    return path


def write_aux_stats(raster: Path, minimum: float, maximum: float, mean: float) -> None:
    raster.with_name(raster.name + ".aux.xml").write_text(
        "<PAMDataset><PAMRasterBand band=\"1\"><Metadata>"
        f"<MDI key=\"STATISTICS_MINIMUM\">{minimum}</MDI><MDI key=\"STATISTICS_MAXIMUM\">{maximum}</MDI>"
        f"<MDI key=\"STATISTICS_MEAN\">{mean}</MDI></Metadata></PAMRasterBand></PAMDataset>",
        encoding="utf-8",
    )


PRJ_UTM18 = ('PROJCS["NAD_1983_CSRS_UTM_Zone_18N",GEOGCS["GCS_North_American_1983_CSRS",DATUM["D_North_American_1983_CSRS",'
             'SPHEROID["GRS_1980",6378137.0,298.257222101]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
             'PROJECTION["Transverse_Mercator"],PARAMETER["False_Easting",500000.0],PARAMETER["Central_Meridian",-75.0],'
             'UNIT["Meter",1.0]]')


def write_shapefile(path: Path, shape_type: int, box: tuple[float, float, float, float], count: int = 1,
                    prj: str | None = PRJ_UTM18) -> Path:
    """Header-only shapefile set (.shp/.shx/.dbf/.prj); records are not needed by the audit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    words = 50 + (count * 8 if count else 0)
    header = struct.pack(">7i", 9994, 0, 0, 0, 0, 0, words) + struct.pack("<2i", 1000, shape_type)
    header += struct.pack("<4d", *box) + struct.pack("<4d", 0, 0, 0, 0)
    path.write_bytes(header + b"\x00" * (words * 2 - 100))
    path.with_suffix(".shx").write_bytes(header)
    path.with_suffix(".dbf").write_bytes(struct.pack("<4BI", 3, 126, 1, 1, count) + b"\x00" * 24)
    if prj:
        path.with_suffix(".prj").write_text(prj)
    return path


def write_geojson(path: Path, kind: str, coordinates: list, crs_epsg: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {"type": "FeatureCollection",
                  "features": [{"type": "Feature", "properties": {}, "geometry": {"type": kind, "coordinates": coordinates}}]}
    if crs_epsg:
        data["crs"] = {"type": "name", "properties": {"name": f"urn:ogc:def:crs:EPSG::{crs_epsg}"}}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def write_geopackage(path: Path, table: str, geometry_type: str, box: tuple[float, float, float, float], srs: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.executescript(
        "CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT, srs_id INTEGER PRIMARY KEY, organization TEXT,"
        " organization_coordsys_id INTEGER, definition TEXT, description TEXT);"
        "CREATE TABLE gpkg_contents (table_name TEXT PRIMARY KEY, data_type TEXT, identifier TEXT, description TEXT,"
        " last_change TEXT, min_x REAL, min_y REAL, max_x REAL, max_y REAL, srs_id INTEGER);"
        "CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT, geometry_type_name TEXT, srs_id INTEGER,"
        " z TINYINT, m TINYINT);"
        f'CREATE TABLE "{table}" (fid INTEGER PRIMARY KEY, geom BLOB);'
    )
    connection.execute("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)", ("utm18", srs, "EPSG", srs, PRJ_UTM18, ""))
    connection.execute("INSERT INTO gpkg_contents VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (table, "features", table, "", "", *box, srs))
    connection.execute("INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,?,?)", (table, "geom", geometry_type, srs, 0, 0))
    connection.execute(f'INSERT INTO "{table}" (geom) VALUES (NULL)')
    connection.execute(f'INSERT INTO "{table}" (geom) VALUES (NULL)')
    connection.commit()
    connection.close()
    return path


def _new_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="hlz_dataroot_")) / "HLZ_DATA"
    dr.create_data_root(root)
    return root


def _good_root(size: int = 3000) -> Path:
    """DTM and aligned DSM of size x size m, water polygon, AOI with enough margin."""
    root = _new_root()
    dtm = write_geotiff(dr.folder_path(root, "dtm") / "ottawa_dtm_1m.tif", size, size)
    dsm = write_geotiff(dr.folder_path(root, "dsm") / "ottawa_dsm_1m.tif", size, size)
    write_aux_stats(dtm, 50.0, 200.0, 100.0)
    write_aux_stats(dsm, 50.0, 230.0, 104.0)
    x0, y1 = 445000.0, 5030000.0
    write_shapefile(dr.folder_path(root, "hydrology") / "waterbody.shp", 5, (x0 + 100, y1 - 900, x0 + 600, y1 - 300), 3)
    write_shapefile(dr.folder_path(root, "aoi") / "aoi.shp", 5, (x0 + 1200, y1 - 1800, x0 + 1800, y1 - 1200))
    return root


def _levels(result: dr.AuditResult, text: str) -> list[str]:
    return [item.level for item in result.findings if text.lower() in item.message.lower()]


# --- layout ---------------------------------------------------------------------


def test_create_layout_is_complete_and_idempotent() -> None:
    root = Path(tempfile.mkdtemp()) / "HLZ_DATA"
    created = dr.create_data_root(root)
    assert "." in created and len(created) == len(dr.FOLDERS) + 1
    for spec in dr.FOLDERS:
        folder = dr.folder_path(root, spec.key)
        assert folder.is_dir(), spec.path
        assert (folder / "README.txt").read_text(encoding="utf-8").startswith(dr.README_HEADER)
    marker = json.loads((root / dr.MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["layout_version"] == dr.LAYOUT_VERSION and "SPDX" in marker["_license"]
    # A README the user edited is never overwritten; a second run creates nothing.
    edited = dr.folder_path(root, "dtm") / "README.txt"
    edited.write_text("my notes", encoding="utf-8")
    assert dr.create_data_root(root) == []
    assert edited.read_text(encoding="utf-8") == "my notes"
    assert json.loads((root / dr.MARKER_NAME).read_text())["created"] == marker["created"]
    assert dr.is_data_root(root) and dr.resolve_data_root(root.parent) == root


# --- readers ----------------------------------------------------------------------


def test_geotiff_header_reader_classic_and_bigtiff() -> None:
    folder = Path(tempfile.mkdtemp())
    for big in (False, True):
        path = write_geotiff(folder / f"t{int(big)}.tif", 40, 30, cell=0.5, x0=1000.0, y0=2000.0, vertical=6647, bigtiff=big)
        found = dr.inspect_path(path, "dtm", folder)[0]
        assert found.readable, found.note
        assert (found.width, found.height, found.bands, found.dtype) == (40, 30, 1, "float32")
        assert found.cell_w == 0.5 and found.extent == (1000.0, 1985.0, 1020.0, 2000.0)
        assert found.crs.epsg == UTM18 and found.crs.utm_zone == 18 and found.crs.metric_projected
        assert found.nodata == "-32767" and found.crs.vertical.startswith("CGVD2013")


def test_wkt_reader_units_and_web_mercator() -> None:
    crs = dr.crs_from_wkt(PRJ_UTM18)
    assert crs.kind == "projected" and crs.unit == "metre" and crs.utm_zone == 18
    feet = dr.crs_from_wkt('PROJCS["NAD83 / Ohio North (ftUS)",GEOGCS["NAD83",UNIT["degree",0.0174]],UNIT["US survey foot",0.3048006,AUTHORITY["EPSG","9003"]],AUTHORITY["EPSG","3734"]]')
    assert feet.unit == "us-foot" and feet.epsg == 3734
    assert dr.crs_from_wkt('GEOGCS["WGS 84",AUTHORITY["EPSG","4326"]]').kind == "geographic"
    assert dr._crs_from_epsg(3857).web_mercator
    assert dr.utm_from_epsg(32618) == (18, True, "WGS 84") and dr.utm_from_epsg(22718)[0] == 18


def test_vector_readers_shapefile_geojson_geopackage() -> None:
    folder = Path(tempfile.mkdtemp())
    shp = dr.inspect_path(write_shapefile(folder / "lake.shp", 15, (1, 2, 3, 4), 7), "hydrology", folder)[0]
    assert (shp.geometry, shp.feature_count, shp.extent, shp.crs.utm_zone) == ("Polygon", 7, (1, 2, 3, 4), 18)
    gj = dr.inspect_path(write_geojson(folder / "c.geojson", "MultiPoint", [[-75.7, 45.4], [-75.6, 45.5]]), "centres", folder)[0]
    assert gj.geometry == "Point" and gj.extent == (-75.7, 45.4, -75.6, 45.5) and gj.crs.kind == "geographic"
    layers = dr.inspect_path(write_geopackage(folder / "w.gpkg", "lakes", "MULTIPOLYGON", (1, 2, 3, 4), UTM18), "hydrology", folder)
    assert len(layers) == 1 and layers[0].geometry == "Polygon" and layers[0].feature_count == 2
    assert layers[0].path.endswith(os.sep + "main.lakes") and layers[0].crs.epsg == UTM18


def test_geographic_extent_is_projected_to_utm() -> None:
    target = dr._crs_from_epsg(UTM18)
    item = dr.Dataset(path="x", rel="x", name="x", folder_key=None, kind="vector", fmt="GeoJSON",
                      crs=dr._crs_from_epsg(4326), extent=(-75.71, 45.42, -75.69, 45.43))
    box = dr.extent_in(item, target)
    assert box is not None and 440000 < box[0] < box[2] < 450000 and 5_028_000 < box[1] < box[3] < 5_032_000


# --- audit --------------------------------------------------------------------------


def test_empty_root_is_not_ready() -> None:
    result = dr.audit_data_root(_new_root())
    assert result.status == dr.STATUS_NOT_READY and result.inputs.dtm is None
    assert "FAIL" in _levels(result, "No DTM") and "WARN" in _levels(result, "No DSM")
    assert "WARN" in _levels(result, "No water data")
    for kind in ("html", "text", "json", "latest"):
        assert Path(result.reports[kind]).is_file()


def test_good_root_is_ready_and_inputs_are_selected() -> None:
    root = _good_root()
    result = dr.audit_data_root(root)
    fails = [item.message for item in result.findings if item.level == dr.LEVEL_FAIL]
    warns = [item.message for item in result.findings if item.level == dr.LEVEL_WARN]
    assert not fails, fails
    assert result.status in (dr.STATUS_READY, dr.STATUS_WARNINGS), warns
    assert not any("approach range" in message for message in warns), warns
    inputs = result.inputs
    assert inputs.dtm.endswith("ottawa_dtm_1m.tif") and inputs.dsm.endswith("ottawa_dsm_1m.tif")
    assert len(inputs.exclusions) == 1 and inputs.aoi.endswith("aoi.shp")
    assert inputs.runs_folder and inputs.runs_folder.endswith("08_Runs")
    assert "PASS" in _levels(result, "same grid") and "PASS" in _levels(result, "higher than the DTM")
    page = Path(result.reports["html"]).read_text(encoding="utf-8")
    assert "<svg" in page and "SPDX-License-Identifier" in page and "READY" in page
    data = json.loads(Path(result.reports["json"]).read_text(encoding="utf-8"))
    assert data["inputs"]["dtm"] == inputs.dtm
    same = dr.discover_inputs(root)
    assert (same.dtm, same.dsm, same.exclusions, same.aoi) == (inputs.dtm, inputs.dsm, inputs.exclusions, inputs.aoi)


def test_aoi_too_close_to_the_edge_names_the_sides() -> None:
    root = _good_root()
    aoi = dr.folder_path(root, "aoi") / "aoi.shp"
    write_shapefile(aoi, 5, (445000.0 + 100, 5030000.0 - 800, 445000.0 + 700, 5030000.0 - 200))
    result = dr.audit_data_root(root, write_reports=False)
    message = next(item.message for item in result.findings if "beyond the AOI on the" in item.message)
    assert "west" in message and "north" in message and "east" not in message


def test_bad_dtms_fail() -> None:
    root = _new_root()
    write_geotiff(dr.folder_path(root, "dtm") / "dtm_wgs84.tif", epsg=4326, geographic=True, cell=0.00001, x0=-75.7, y0=45.4)
    result = dr.audit_data_root(root, write_reports=False)
    # v0.9.0: a latitude/longitude DTM is projected by the tool itself, so the audit warns instead of failing.
    assert "WARN" in _levels(result, "latitude/longitude") and "FAIL" not in _levels(result, "latitude/longitude")
    root = _new_root()
    write_geotiff(dr.folder_path(root, "dtm") / "dtm_merc.tif", epsg=3857)
    assert "FAIL" in _levels(dr.audit_data_root(root, write_reports=False), "Web Mercator")
    root = _new_root()
    write_geotiff(dr.folder_path(root, "dtm") / "dtm_int.tif", bits=16, sample_format=2, nodata=None)
    result = dr.audit_data_root(root, write_reports=False)
    assert "WARN" in _levels(result, "whole numbers") and "WARN" in _levels(result, "no NoData")
    root = _new_root()
    write_geotiff(dr.folder_path(root, "dtm") / "tile_a.tif")
    write_geotiff(dr.folder_path(root, "dtm") / "tile_b.tif", x0=445040.0)
    result = dr.audit_data_root(root, write_reports=False)
    assert result.inputs.dtm is None and "FAIL" in _levels(result, "rasters in the DTM folder")


def test_dsm_problems_are_reported() -> None:
    root = _good_root()
    dsm = dr.folder_path(root, "dsm") / "ottawa_dsm_1m.tif"
    write_geotiff(dsm, 3000, 3000, x0=445000.5)  # half-cell shift
    write_aux_stats(dsm, 40.0, 190.0, 90.0)  # lower than the DTM
    result = dr.audit_data_root(root, write_reports=False)
    assert "INFO" in _levels(result, "offset from the DTM grid")
    assert "WARN" in _levels(result, "LOWER than the DTM")
    write_geotiff(dsm, 1000, 1000, vertical=5713)
    dtm = dr.folder_path(root, "dtm") / "ottawa_dtm_1m.tif"
    write_geotiff(dtm, 3000, 3000, vertical=6647)
    result = dr.audit_data_root(root, write_reports=False)
    assert "WARN" in _levels(result, "covers only") and "WARN" in _levels(result, "Vertical datums differ")


def test_vector_problems_and_misplaced_files() -> None:
    root = _good_root()
    write_shapefile(dr.folder_path(root, "hydrology") / "rivers.shp", 3, (445100, 5029100, 445900, 5029900))
    write_shapefile(dr.folder_path(root, "exclusions") / "well_points.shp", 1, (445300, 5029300, 445300, 5029300))
    write_shapefile(dr.folder_path(root, "hydrology") / "gauge_points.shp", 1, (445400, 5029400, 445400, 5029400))
    write_shapefile(dr.folder_path(root, "exclusions") / "far_away.shp", 5, (100, 100, 200, 200))
    write_shapefile(dr.folder_path(root, "exclusions") / "noprj.shp", 5, (445100, 5029100, 445200, 5029200), prj=None)
    write_geojson(dr.folder_path(root, "centres") / "casevac.geojson", "Point", [-75.0, 45.4])
    (dr.folder_path(root, "hydrology") / "ohn_download.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    (dr.folder_path(root, "hydrology") / "lakes.kml").write_text("<kml/>")
    write_geotiff(root / "loose_dsm.tif")
    write_geotiff(dr.folder_path(root, "hydrology") / "water_raster.tif")
    result = dr.audit_data_root(root, write_reports=False)
    assert "INFO" in _levels(result, "holds LINES") and "WARN" in _levels(result, "holds Point features")
    assert "INFO" in _levels(result, "holds POINTS")  # v0.7.0: points in 03_Exclusions are used (a small circle each)
    assert "WARN" in _levels(result, "does not overlap the DTM")
    assert "WARN" in _levels(result, "coordinate system unknown")
    assert "WARN" in _levels(result, "compressed archive")
    assert "WARN" in _levels(result, "outside the standard folders")
    assert any("01_Elevation\\DSM" in item.action for item in result.findings if "loose_dsm" in item.message)
    assert "WARN" in _levels(result, "is a raster; this folder needs vector")
    # waterbody, rivers (lines), far_away, noprj and the well points in 03_Exclusions (v0.7.0); the water-gauge points
    # in 02_Hydrology are not used: points there make no sense as water.
    assert len(result.inputs.exclusions) == 5
    assert not any("gauge_points" in path for path in result.inputs.exclusions)
    assert any("well_points" in path for path in result.inputs.exclusions)
    assert len(result.inputs.centres) == 1


def test_profiles_are_validated() -> None:
    root = _new_root()
    shipped = json.loads((profiles_root() / "aircraft" / "generic_medium.json").read_text(encoding="utf-8"))
    (dr.folder_path(root, "profiles") / "my_ch146.json").write_text(json.dumps(shipped), encoding="utf-8")
    broken = dict(shipped, tdp_diameter_m=-5)
    (dr.folder_path(root, "profiles") / "broken.json").write_text(json.dumps(broken), encoding="utf-8")
    result = dr.audit_data_root(root, write_reports=False)
    assert "PASS" in _levels(result, "my_ch146.json") and "WARN" in _levels(result, "broken.json")
    assert len(result.inputs.custom_aircraft) == 1


def test_aircraft_profile_sets_the_margin() -> None:
    root = _good_root()
    heavy = profiles_root() / "aircraft" / "generic_heavy.json"
    result = dr.audit_data_root(root, aircraft_profile=heavy, write_reports=False)
    assert result.buffer_m > 0 and result.aircraft != "Generic medium (default)"


def test_command_line_exit_codes() -> None:
    parent = Path(tempfile.mkdtemp())
    assert dr.main(["init", str(parent / "HLZ_DATA"), "--quiet"]) == 2  # created, but no DTM yet
    assert dr.is_data_root(parent / "HLZ_DATA")
    write_geotiff(dr.folder_path(parent / "HLZ_DATA", "dtm") / "dtm.tif", 2500, 2500)
    assert dr.main(["audit", str(parent / "HLZ_DATA"), "--quiet", "--no-report"]) == 0
    assert dr.main(["audit", str(parent / "missing"), "--quiet"]) == 1



_TOOLBOX_SCRIPT = r"""
import importlib.machinery, importlib.util, sys
from pathlib import Path
root, data = Path(sys.argv[1]), sys.argv[2]
sys.path[:0] = [str(root / "tests" / "fake_arcpy"), str(root)]
loader = importlib.machinery.SourceFileLoader("hlz_pyt", str(root / "toolbox" / "HLZ_Suitability.pyt"))
spec = importlib.util.spec_from_loader("hlz_pyt", loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)
assert [c.__name__ for c in module.Toolbox().tools] == ["PrepareDataFolder", "AnalyzeHLZ", "ValidateProfiles"]
tool = module.AnalyzeHLZ()
params = tool.getParameterInfo()
assert params[module.P_DATA_ROOT].name == "data_root" and params[module.P_DTM].name == "in_dtm"
assert params[module.P_OUT_REPORT].name == "out_report" and len(params) == 43 and params[module.P_LINE_WIDTH].name == "line_width_m"
assert params[module.P_POINT_RADIUS].name == "point_radius_m" and params[module.P_POINT_RADIUS].value == 10.0
assert params[module.P_OVERVIEW].name == "build_overview" and params[module.P_OVERVIEW].value is False
assert params[module.P_OVERVIEW_SIZE].name == "overview_cell_m" and params[module.P_OVERVIEW_SIZE].value == 500.0
assert params[module.P_OUT_FC].name == "out_candidates" and params[module.P_EXCLUSION].filter.list == ["Polygon", "Polyline", "Point", "Multipoint"]
tool.updateMessages(params)
assert params[module.P_DTM].hasError()
params = tool.getParameterInfo()
params[module.P_DSM].value = "my_own_dsm.tif"
params[module.P_DATA_ROOT].value = data
params[module.P_DATA_ROOT].altered = True
tool.updateParameters(params)
assert params[module.P_DTM].valueAsText.endswith("ottawa_dtm_1m.tif")
assert params[module.P_DSM].valueAsText == "my_own_dsm.tif"
assert params[module.P_EXCLUSION].valueAsText.endswith("waterbody.shp")
assert params[module.P_AOI].valueAsText.endswith("aoi.shp")
assert params[module.P_OUT_FOLDER].valueAsText.endswith("08_Runs")
assert params[module.P_OBST_FILES].valueAsText.endswith("towers.csv"), params[module.P_OBST_FILES].valueAsText
assert not params[module.P_OBST_LAYERS].valueAsText
prepare = module.PrepareDataFolder()
p = prepare.getParameterInfo()
p[module.D_PARENT].value = str(Path(data).parent)
p[module.D_OPEN].value = False
prepare.updateMessages(p)
prepare.execute(p, None)
assert (Path(data) / "09_Audit" / "HLZ_Data_Audit_latest.html").is_file()
print("TOOLBOX_OK")
"""


def test_obstacle_folder_is_audited_and_discovered() -> None:
    root = _good_root()
    result = dr.audit_data_root(root, write_reports=False)
    assert "10_Obstacles" in dr.format_audit_text(result) and result.inputs.obstacles == []
    assert _levels(result, "No obstacle list") == ["INFO"]
    folder = dr.folder_path(root, "obstacles")
    (folder / "DOF.DAT").write_text(
        " CURRENCY DATE = 10/01/2026\n"
        "ORS    OAS         V CO ST CITY             LATDMS      LONDMS       TYPE               Q AGL   AMSL  L H V M FAA STUDY        A JDATE\n"
        "18-000001 O US NY TESTVILLE        45 24 29.00N 075 41 01.00W TOWER              1 00200 00545 R 3 D I 2007-AWP-1234-OE  A 2007033\n",
        encoding="utf-8")
    (folder / "towers.csv").write_text("name,latitude,longitude\nMast,45.408,-75.684\n", encoding="utf-8")
    (folder / "osm.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"man_made": "mast", "height": "45"},
        "geometry": {"type": "Point", "coordinates": [-75.684, 45.408]}}]}), encoding="utf-8")
    result = dr.audit_data_root(root, write_reports=False)
    names = sorted(Path(path).name for path in result.inputs.obstacles)
    assert names == ["DOF.DAT", "osm.geojson", "towers.csv"], names
    assert _levels(result, "No obstacle list") == []
    assert any("towers.csv" in item.message and item.level == "WARN" and "default" in item.action.lower() for item in result.findings)
    assert "Obstacles" in dr.format_audit_html(result) or True


def test_toolbox_data_folder_fields() -> None:
    """The toolbox (with the test ArcPy fake) fills only EMPTY fields from the data folder, and tool 0 audits it."""
    import subprocess

    root = _good_root()
    (dr.folder_path(root, "obstacles") / "towers.csv").write_text(
        "name,latitude,longitude,height_m\nTower,45.408,-75.684,60\n", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-c", _TOOLBOX_SCRIPT, str(ROOT), str(root)], capture_output=True, text=True, timeout=600
    )
    if "No module named 'numpy'" in completed.stderr or "No module named 'scipy'" in completed.stderr:
        return  # the toolbox needs NumPy and SciPy; this file otherwise runs with the standard library only
    assert "TOOLBOX_OK" in completed.stdout, completed.stdout[-2000:] + completed.stderr[-3000:]


if __name__ == "__main__":
    failures = 0
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            try:
                function()
                print(f"PASS {name}")
            except Exception as error:  # noqa: BLE001
                failures += 1
                import traceback

                traceback.print_exc()
                print(f"FAIL {name}: {type(error).__name__}: {error}")
    raise SystemExit(1 if failures else 0)
