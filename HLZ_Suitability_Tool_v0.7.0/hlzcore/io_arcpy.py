# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""ArcGIS Pro (ArcPy) adapter for the HLZ engine.

Everything here uses ArcPy plus NumPy/SciPy/matplotlib, which all ship with
the default ``arcgispro-py3`` environment. No extension licence (Spatial
Analyst etc.) is required.

Responsibilities:
* read the DTM/DSM window (AOI + approach buffer) into NumPy arrays,
* align a mismatched DSM onto the DTM grid (Resample/Project, bilinear),
* rasterise the AOI polygon into a candidate mask,
* write rasters and feature classes into a file geodatabase,
* add the results to the active map with readable symbology and labels.
"""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from typing import Any, Callable

import numpy as np

import arcpy  # type: ignore[import-not-found]

from .geometry_utils import approach_line, circle_ring, sector_ring
from .models import AircraftProfile, Candidate, GridGeometry
from .masks import rasterize_lines, rasterize_points, rasterize_polygons
from .overview import OVERVIEW_FIELDS, OverviewResult
from .search import SearchArea, combine_masks, intersect_windows
from .obstacles import (
    DEFAULT_OBSTACLE_BUFFER_M,
    DEFAULT_OBSTACLE_HEIGHT_M,
    HEIGHT_FIELD_CANDIDATES,
    NAME_FIELD_CANDIDATES,
    LoadResult,
    Obstacle,
    ObstacleError,
    finish_load,
    find_field,
    parse_height,
    read_obstacle_file,
)
from .pipeline import AnalysisError
from .report import RATING_RGB, SECTOR_RGB

FEET_TO_METRES = 0.3048
DEFAULT_LINE_WIDTH_M = 10.0  # total width given to river/stream/shoreline LINES used as exclusion areas
DEFAULT_POINT_RADIUS_M = 10.0  # v0.7.0: radius given to POINTS used as exclusion areas (a known hazard, a pole, a crane)
LOGGER = logging.getLogger("hlz")

GROUND_CLASS_STYLE = {
    "0": ("Fails ground criteria", (211, 47, 47, 0)),
    "1": ("Passes - any landing heading", (92, 184, 92, 100)),
    "2": ("Passes - upslope landing only", (240, 173, 0, 100)),
}


# ---------------------------------------------------------------------------
# Raster description and validation (also used by the toolbox UI)
# ---------------------------------------------------------------------------


def raster_info(path: str) -> dict[str, Any]:
    """Return the raster properties needed for validation."""
    catalog_path = arcpy.Describe(path).catalogPath
    raster = arcpy.Raster(catalog_path)
    spatial_reference = raster.spatialReference
    return {
        "catalog_path": catalog_path,
        "band_count": int(raster.bandCount),
        "width": int(raster.width),
        "height": int(raster.height),
        "cell_w": float(raster.meanCellWidth),
        "cell_h": float(raster.meanCellHeight),
        "extent": raster.extent,
        "sr": spatial_reference,
        "sr_name": spatial_reference.name if spatial_reference else "",
        "sr_type": spatial_reference.type if spatial_reference else "",
        "linear_unit": (spatial_reference.linearUnitName or "") if spatial_reference else "",
        "meters_per_unit": float(getattr(spatial_reference, "metersPerUnit", 0.0) or 0.0),
        "pixel_type": str(raster.pixelType),
        "nodata": raster.noDataValue,
    }


def dtm_problems(info: dict[str, Any]) -> list[tuple[str, str]]:
    """Return [(level, message)] with level 'error' or 'warning'."""
    problems: list[tuple[str, str]] = []
    if info["band_count"] != 1:
        problems.append(("error", "The DTM must be a single-band elevation raster."))
    if info["sr_type"] != "Projected":
        problems.append(
            (
                "error",
                "The DTM uses a geographic (lat/long) coordinate system. Run 'Project Raster' to a "
                "projected system in metres (e.g. the local UTM zone) first.",
            )
        )
    elif "meter" not in info["linear_unit"].lower() and abs(info["meters_per_unit"] - 1.0) > 1.0e-6:
        problems.append(
            (
                "error",
                f"The DTM horizontal unit is '{info['linear_unit']}'. Project it to a coordinate "
                "system in metres (e.g. UTM).",
            )
        )
    if info["sr_type"] == "Projected" and any(
        tag in (info.get("sr_name") or "").lower() for tag in ("pseudo-mercator", "pseudo_mercator", "web_mercator", "web mercator")
    ):
        problems.append(
            (
                "error",
                "The DTM is in Web Mercator, which stretches distances by about 41% at Ottawa's latitude. Run "
                "'Project Raster' to the local UTM zone first.",
            )
        )
    if info["cell_w"] > 0 and abs(info["cell_w"] - info["cell_h"]) / info["cell_w"] > 0.01:
        problems.append(("error", "DTM cells must be square (equal X and Y cell size)."))
    if info["cell_w"] > 2.5:
        problems.append(
            (
                "warning",
                f"Cell size {info['cell_w']:.2f} m is coarse; results are screening quality only "
                "(1 m or finer recommended).",
            )
        )
    return problems


def same_spatial_reference(first: Any, second: Any) -> bool:
    if first is None or second is None:
        return False
    if first.factoryCode and second.factoryCode:
        return int(first.factoryCode) == int(second.factoryCode)
    return first.name == second.name


# ---------------------------------------------------------------------------
# Reading inputs
# ---------------------------------------------------------------------------


def _read_window(path: str, lower_left: Any, ncols: int, nrows: int) -> tuple[np.ndarray, np.ndarray]:
    """Read a raster window as float32 with a reliable validity mask.

    Early versions compared values to ``raster.noDataValue`` with exact float equality.
    In a file geodatabase the float NoData sentinel (~-3.4e38) is reported
    with a different rounding than the array value, so NoData cells passed as
    valid terrain. The reader now asks ArcPy to write NaN into NoData cells for floating
    rasters and compares in the raster's own dtype for integer rasters.
    """
    raster = arcpy.Raster(path)
    nodata = raster.noDataValue
    pixel_type = str(raster.pixelType).upper()
    if pixel_type.startswith("F"):
        array = arcpy.RasterToNumPyArray(raster, lower_left, ncols, nrows, nodata_to_value=np.nan)
        values = np.asarray(array, dtype=np.float32)
        valid = np.isfinite(values)
    else:
        array = arcpy.RasterToNumPyArray(raster, lower_left, ncols, nrows)
        valid = np.ones(array.shape, dtype=bool)
        if nodata is not None:
            try:
                sentinel = np.array(nodata).astype(array.dtype)
                valid &= array != sentinel
            except (OverflowError, ValueError):
                pass
        values = array.astype(np.float32)
        del array
    values[~valid] = np.nan
    return values, valid


def latlon_to_xy(latitude: float, longitude: float, target_sr: Any) -> tuple[float, float]:
    """WGS84 latitude/longitude to x, y in ``target_sr`` (uses the best datum transformation)."""
    wgs84 = arcpy.SpatialReference(4326)
    point = arcpy.PointGeometry(arcpy.Point(longitude, latitude), wgs84)
    transformation = ""
    try:
        extent = arcpy.Extent(longitude - 0.01, latitude - 0.01, longitude + 0.01, latitude + 0.01, spatial_reference=wgs84)
        transformations = arcpy.ListTransformations(wgs84, target_sr, extent)
        if transformations:
            transformation = transformations[0]
    except Exception:  # noqa: BLE001
        transformation = ""
    projected = point.projectAs(target_sr, transformation) if transformation else point.projectAs(target_sr)
    return float(projected.firstPoint.X), float(projected.firstPoint.Y)


def aoi_geometry(aoi: str, spatial_reference: Any) -> Any | None:
    """Union of all AOI polygons (selected features only, if any), projected."""
    union = None
    with arcpy.da.SearchCursor(aoi, ["SHAPE@"]) as cursor:
        for (shape,) in cursor:
            if shape is None:
                continue
            if not same_spatial_reference(shape.spatialReference, spatial_reference):
                shape = shape.projectAs(spatial_reference)
            union = shape if union is None else union.union(shape)
    return union


def view_extent_polygon(target_sr: Any | None = None) -> Any | None:
    """Return the active map view extent as a polygon, or None.

    Works only when the tool runs from an open ArcGIS Pro project with a map
    view active. The polygon is projected to ``target_sr`` when given.
    """
    try:
        project = arcpy.mp.ArcGISProject("CURRENT")
        view = project.activeView
        camera = getattr(view, "camera", None)
        if camera is None:
            return None
        extent = camera.getExtent()
        if extent is None or not extent.width or not extent.height:
            return None
        polygon = extent.polygon
        if polygon.spatialReference is None or not polygon.spatialReference.name:
            polygon = arcpy.Polygon(polygon.getPart(0), extent.spatialReference)
        if target_sr is not None and not same_spatial_reference(polygon.spatialReference, target_sr):
            polygon = polygon.projectAs(target_sr)
        return polygon
    except Exception:  # noqa: BLE001 - not inside Pro, or no map view active
        return None


def rasterize_polygon(polygon: Any, geometry: GridGeometry) -> np.ndarray:
    """Boolean mask of cell centres inside a polygon (holes respected)."""
    from matplotlib.path import Path as MplPath

    mask = np.zeros((geometry.height, geometry.width), dtype=bool)
    extent = polygon.extent
    col0 = max(0, int(math.floor((extent.XMin - geometry.x_origin) / geometry.pixel_width)))
    col1 = min(geometry.width, int(math.ceil((extent.XMax - geometry.x_origin) / geometry.pixel_width)) + 1)
    row0 = max(0, int(math.floor((extent.YMax - geometry.y_origin) / geometry.pixel_height)))
    row1 = min(geometry.height, int(math.ceil((extent.YMin - geometry.y_origin) / geometry.pixel_height)) + 1)
    if col1 <= col0 or row1 <= row0:
        return mask
    rows, cols = np.mgrid[row0:row1, col0:col1]
    xs = geometry.x_origin + (cols + 0.5) * geometry.pixel_width
    ys = geometry.y_origin + (rows + 0.5) * geometry.pixel_height
    points = np.column_stack([xs.ravel(), ys.ravel()])
    inside = np.zeros(len(points), dtype=bool)
    for part in polygon:
        ring: list[tuple[float, float]] = []
        for point in part:
            if point is None:  # ring separator inside a part
                if len(ring) >= 3:
                    inside ^= MplPath(ring).contains_points(points)
                ring = []
            else:
                ring.append((point.X, point.Y))
        if len(ring) >= 3:
            inside ^= MplPath(ring).contains_points(points)
    mask[row0:row1, col0:col1] = inside.reshape(rows.shape)
    return mask


def latlon_projector(target_sr: Any) -> Callable[[list[float], list[float]], tuple[list[float], list[float]]]:
    """A function (lons, lats) -> (xs, ys) in ``target_sr``; the datum transformation is chosen once."""
    wgs84 = arcpy.SpatialReference(4326)
    transformation = ""
    try:
        transformations = arcpy.ListTransformations(wgs84, target_sr)
        if transformations:
            transformation = transformations[0]
    except Exception:  # noqa: BLE001
        transformation = ""

    def project(lons: list[float], lats: list[float]) -> tuple[list[float], list[float]]:
        xs: list[float] = []
        ys: list[float] = []
        for lon, lat in zip(lons, lats):
            point = arcpy.PointGeometry(arcpy.Point(lon, lat), wgs84)
            moved = point.projectAs(target_sr, transformation) if transformation else point.projectAs(target_sr)
            xs.append(float(moved.firstPoint.X))
            ys.append(float(moved.firstPoint.Y))
        return xs, ys

    return project


def read_obstacle_layer(
    layer: str,
    target_sr: Any,
    bounds: tuple[float, float, float, float],
    height_field: str | None,
    height_units: str,
    default_height_m: float,
) -> LoadResult:
    """Obstacles from a GIS layer (shapefile, GeoPackage, geodatabase), returned in the DTM CRS.

    ``bounds`` is (xmin, ymin, xmax, ymax) in the DTM CRS; features outside it are counted and dropped.
    The height is read from ``height_field`` or an auto-detected field; features without a height get the default.
    """
    description = arcpy.Describe(layer)
    shape_type = getattr(description, "shapeType", "")
    if shape_type not in ("Point", "Multipoint", "Polyline", "Polygon"):
        raise ObstacleError(f"Obstacle layer '{layer}' holds {shape_type or 'unknown'} features; use points, lines or polygons.")
    names = [field.name for field in arcpy.ListFields(layer)]
    if height_field and not find_field(names, height_field, ()):
        raise ObstacleError(f"Obstacle layer '{layer}' has no field '{height_field}'. Fields found: {', '.join(names)}.")
    height_name = find_field(names, height_field, HEIGHT_FIELD_CANDIDATES)
    name_name = find_field(names, None, NAME_FIELD_CANDIDATES)
    fields = ["SHAPE@"] + ([height_name] if height_name else []) + ([name_name] if name_name else [])
    label = getattr(description, "baseName", "") or str(layer)
    result = LoadResult(source=str(label), height_field=height_name)
    x_min, y_min, x_max, y_max = bounds
    try:
        cursor = arcpy.da.SearchCursor(layer, fields, spatial_reference=target_sr)
    except TypeError:
        cursor = arcpy.da.SearchCursor(layer, fields)
    with cursor:
        for row in cursor:
            shape = row[0]
            result.records += 1
            if shape is None:
                result.skipped += 1
                continue
            box = shape.extent
            if box.XMax < x_min or box.XMin > x_max or box.YMax < y_min or box.YMin > y_max:
                result.outside += 1
                continue
            index = 1
            value = None
            if height_name:
                value = row[index]
                index += 1
            text = str(row[index] or "").strip() if name_name else ""
            height = parse_height(value, height_units, height_name) if height_name else None
            assumed = height is None
            parts: list[list[tuple[float, float]]] = []
            if shape_type in ("Point", "Multipoint"):
                for part in shape:
                    for point in (part if hasattr(part, "__iter__") else [part]):
                        if point is not None:
                            parts.append([(point.X, point.Y)])
                kind = "point"
            else:
                for part in shape:
                    ring: list[tuple[float, float]] = []
                    for point in part:
                        if point is None:  # separates the interior rings of a polygon part
                            if len(ring) >= (3 if shape_type == "Polygon" else 2):
                                parts.append(ring)
                            ring = []
                        else:
                            ring.append((point.X, point.Y))
                    if len(ring) >= (3 if shape_type == "Polygon" else 2):
                        parts.append(ring)
                kind = "polygon" if shape_type == "Polygon" else "line"
            if not parts:
                result.skipped += 1
                continue
            if kind == "point":
                for part in parts:
                    result.obstacles.append(Obstacle("point", [part], default_height_m if assumed else height, text, result.source, assumed))
                    result.assumed += int(assumed)
            else:
                result.obstacles.append(Obstacle(kind, parts, default_height_m if assumed else height, text, result.source, assumed))
                result.assumed += int(assumed)
    if result.records == 0:
        raise ObstacleError(f"Obstacle layer '{layer}' holds no features.")
    return result


class ArcpyTileReader:
    """``TileReader`` over ArcGIS rasters: windows are read on demand.

    Memory use is bounded by one tile plus its approach buffer, whatever the
    size of the DTM. An AOI polygon (layer or geometry) limits where tiles are
    placed; tiles outside it are skipped and tiles fully inside need no mask.
    A DSM on a different grid is resampled per read window (bilinear).
    """

    def __init__(
        self,
        dtm: str,
        dsm: str | None,
        aoi: Any,
        z_units: str,
        scratch_workspace: str,
        exclusion_layers: list[str] | None = None,
        search_centres: list[tuple[float, float]] | None = None,
        search_radius_m: float | None = None,
        log: Callable[[str], None] = LOGGER.info,
        line_width_m: float = DEFAULT_LINE_WIDTH_M,
        point_radius_m: float = DEFAULT_POINT_RADIUS_M,
        obstacle_layers: list[str] | None = None,
        obstacle_files: list[str] | None = None,
        obstacle_height_field: str | None = None,
        obstacle_height_units: str = "Meters",
        obstacle_default_height_m: float = DEFAULT_OBSTACLE_HEIGHT_M,
        obstacle_buffer_m: float = DEFAULT_OBSTACLE_BUFFER_M,
    ):
        info = raster_info(dtm)
        for level, message in dtm_problems(info):
            if level == "error":
                raise AnalysisError(message)
        self.info = info
        self.sr = info["sr"]
        self.scratch = scratch_workspace
        self.log = log
        self.notes: list[str] = []
        self.z_scale = FEET_TO_METRES if z_units.upper().startswith("F") else 1.0
        extent = info["extent"]
        self.cell_w = info["cell_w"]
        self.cell_h = info["cell_h"]
        self.geometry = GridGeometry(
            x_origin=extent.XMin,
            y_origin=extent.YMax,
            pixel_width=self.cell_w,
            pixel_height=-self.cell_h,
            width=info["width"],
            height=info["height"],
        )
        self.crs_description = f"{self.sr.name} (WKID {self.sr.factoryCode})"
        self.dtm_source = info["catalog_path"]

        # AOI ---------------------------------------------------------------
        self.polygon = None
        if isinstance(aoi, str) and aoi:
            self.polygon = aoi_geometry(aoi, self.sr)
            if self.polygon is None:
                raise AnalysisError("The AOI layer contains no polygons.")
        elif aoi is not None and not isinstance(aoi, str):
            polygon = aoi
            if not same_spatial_reference(polygon.spatialReference, self.sr):
                polygon = polygon.projectAs(self.sr)
            self.polygon = polygon
        if self.polygon is not None:
            box = self.polygon.extent
            col0 = max(0, int(math.floor((box.XMin - extent.XMin) / self.cell_w)))
            col1 = min(info["width"], int(math.ceil((box.XMax - extent.XMin) / self.cell_w)))
            row0 = max(0, int(math.floor((extent.YMax - box.YMax) / self.cell_h)))
            row1 = min(info["height"], int(math.ceil((extent.YMax - box.YMin) / self.cell_h)))
            if col1 <= col0 or row1 <= row0:
                raise AnalysisError("The area of interest does not overlap the DTM.")
            self.window = (row0, row1, col0, col1)
            self.aoi_digest = (
                f"polygon:{round(box.XMin, 2)},{round(box.YMin, 2)},{round(box.XMax, 2)},"
                f"{round(box.YMax, 2)}:area={round(self.polygon.area, 1)}:points={self.polygon.pointCount}"
            )
        else:
            self.window = (0, info["height"], 0, info["width"])
            self.aoi_digest = "none"

        # DSM ---------------------------------------------------------------
        self.has_dsm = bool(dsm)
        self.dsm_source = None
        self.dsm_path = None
        self.dsm_aligned = True
        if dsm:
            dsm_info = raster_info(dsm)
            if dsm_info["band_count"] != 1:
                raise AnalysisError("The DSM must be a single-band raster.")
            self.dsm_source = dsm_info["catalog_path"]
            self.dsm_path = dsm_info["catalog_path"]
            self.dsm_aligned = (
                same_spatial_reference(dsm_info["sr"], self.sr)
                and math.isclose(dsm_info["cell_w"], self.cell_w, rel_tol=1.0e-6)
                and math.isclose(dsm_info["cell_h"], self.cell_h, rel_tol=1.0e-6)
                and _is_multiple(dsm_info["extent"].XMin - extent.XMin, self.cell_w)
                and _is_multiple(dsm_info["extent"].YMax - extent.YMax, self.cell_h)
            )
            self.dsm_same_sr = same_spatial_reference(dsm_info["sr"], self.sr)
            if not self.dsm_aligned:
                self.notes.append(
                    "DSM grid differs from the DTM: it is resampled (bilinear) onto the DTM grid tile by "
                    "tile. For large areas, snapping the DSM to the DTM once beforehand is faster."
                )

        # Search around a point: circles that limit where landing zones are placed -----------
        self.search_area = None
        if search_centres and search_radius_m:
            self.search_area = SearchArea(search_centres, search_radius_m)
            circle_window = self.search_area.window(self.geometry)
            combined = intersect_windows(self.window, circle_window) if circle_window else None
            if combined is None:
                where = " inside the area of interest" if self.polygon is not None else ""
                raise AnalysisError(f"The search circle does not overlap the DTM{where}.")
            self.window = combined
            self.aoi_digest = f"{self.aoi_digest}|{self.search_area.digest}"

        # Exclusion areas (polygons the touchdown point must not touch) ----------------
        self.exclusion_layers = [layer for layer in (exclusion_layers or []) if layer]
        self.has_exclusion = bool(self.exclusion_layers)
        self.line_width_m = float(line_width_m)
        self.point_radius_m = float(point_radius_m)
        counts = []
        self.exclusion_kinds: dict[str, str] = {}
        for layer in self.exclusion_layers:
            shape_type = getattr(arcpy.Describe(layer), "shapeType", "")
            if shape_type not in ("Polygon", "Polyline", "Point", "Multipoint"):
                raise AnalysisError(
                    f"Exclusion layer '{layer}' holds {shape_type or 'unknown'} features. Use polygons (lakes, "
                    "wetlands), lines (rivers, streams, shorelines) or points (known hazards)."
                )
            self.exclusion_kinds[layer] = shape_type
            counts.append(int(arcpy.management.GetCount(layer)[0]))
        any_lines = "Polyline" in self.exclusion_kinds.values()
        any_points = any(kind in ("Point", "Multipoint") for kind in self.exclusion_kinds.values())

        def unit_name(kind: str) -> str:
            return {"Polyline": "lines", "Point": "points", "Multipoint": "points"}.get(kind, "features")

        self.exclusion_description = "; ".join(
            f"{layer} ({count:,} {unit_name(self.exclusion_kinds[layer])})"
            for layer, count in zip(self.exclusion_layers, counts)
        ) or "none"
        if any_lines:
            self.exclusion_description += f" | lines widened to {self.line_width_m:g} m"
        if any_points:
            self.exclusion_description += f" | points given a {self.point_radius_m:g} m radius"
        self.exclusion_digest = "|".join(f"{layer}:{count}" for layer, count in zip(self.exclusion_layers, counts)) or "none"
        if any_lines:
            self.exclusion_digest += f"|line_width:{self.line_width_m:g}"
        if any_points:
            self.exclusion_digest += f"|point_radius:{self.point_radius_m:g}"

        self._load_obstacles(
            [layer for layer in (obstacle_layers or []) if layer],
            [path for path in (obstacle_files or []) if path],
            obstacle_height_field, obstacle_height_units, obstacle_default_height_m, obstacle_buffer_m,
        )

    def _load_obstacles(
        self,
        layers: list[str],
        files: list[str],
        height_field: str | None,
        height_units: str,
        default_height_m: float,
        buffer_m: float,
    ) -> None:
        """Listed obstacles: GIS layers (read in the DTM CRS) and FAA DOF / CSV / GeoJSON files (WGS84)."""
        self.obstacles = None
        self.obstacle_description = "none"
        self.obstacle_digest = "none"
        if not layers and not files:
            return
        from .obstacles import project_obstacles

        extent = self.info["extent"]
        margin = 5000.0 if not getattr(self.sr, "type", "") == "Geographic" else 0.05
        bounds = (extent.XMin - margin, extent.YMin - margin, extent.XMax + margin, extent.YMax + margin)
        results: list[LoadResult] = []
        projected: list[Obstacle] = []
        labels: list[str] = []
        try:
            for layer in layers:
                result = read_obstacle_layer(layer, self.sr, bounds, height_field, height_units, default_height_m)
                results.append(result)
                projected.extend(result.obstacles)
                labels.append(result.source)
            if files:
                wgs84 = arcpy.SpatialReference(4326)
                box = arcpy.Extent(*bounds, spatial_reference=self.sr).polygon.projectAs(wgs84).extent
                bbox_ll = (box.XMin, box.YMin, box.XMax, box.YMax)
                project = latlon_projector(self.sr)
                for path in files:
                    result = read_obstacle_file(path, height_field, height_units, default_height_m, bbox_ll)
                    results.append(result)
                    projected.extend(project_obstacles(result.obstacles, project))
                    labels.append(Path(path).name)
        except ObstacleError as error:
            raise AnalysisError(str(error)) from error
        load = finish_load(projected, results, labels, buffer_m, abs(self.cell_w))
        self.obstacles = load.obstacles
        self.obstacle_description = load.description
        self.obstacle_digest = load.digest
        self.notes.extend(load.notes)

    def exclusion_mask(self, row0: int, row1: int, col0: int, col1: int) -> np.ndarray | None:
        """Boolean mask of the cells covered by the exclusion polygons (None if no layer)."""
        if not self.has_exclusion:
            return None
        extent = self.info["extent"]
        x_min = extent.XMin + col0 * self.cell_w
        x_max = extent.XMin + col1 * self.cell_w
        y_max = extent.YMax - row0 * self.cell_h
        y_min = extent.YMax - row1 * self.cell_h
        grid = GridGeometry(x_min, y_max, self.cell_w, -self.cell_h, col1 - col0, row1 - row0)
        box = arcpy.Extent(x_min, y_min, x_max, y_max, spatial_reference=self.sr)
        polygons: list[Any] = []
        lines: list[Any] = []
        points: list[tuple[float, float]] = []
        # Lines count when they are within half their width of the window, so read a slightly larger box.
        margin = self.line_width_m / 2.0 + 2.0 * self.cell_w
        line_box = arcpy.Extent(x_min - margin, y_min - margin, x_max + margin, y_max + margin, spatial_reference=self.sr)
        # Points count when they are within their radius of the window.
        point_margin = self.point_radius_m + 2.0 * self.cell_w
        point_box = arcpy.Extent(
            x_min - point_margin, y_min - point_margin, x_max + point_margin, y_max + point_margin, spatial_reference=self.sr
        )
        for layer in self.exclusion_layers:
            kind = self.exclusion_kinds.get(layer)
            if kind == "Polyline":
                lines.extend(self._exclusion_lines(layer, line_box, (x_min - margin, y_min - margin, x_max + margin, y_max + margin)))
            elif kind in ("Point", "Multipoint"):
                points.extend(self._exclusion_points(layer, point_box, (x_min - point_margin, y_min - point_margin, x_max + point_margin, y_max + point_margin)))
            else:
                polygons.extend(self._exclusion_polygons(layer, box, (x_min, y_min, x_max, y_max)))
        mask = rasterize_polygons(polygons, grid) if polygons else np.zeros((row1 - row0, col1 - col0), dtype=bool)
        if lines:
            mask |= rasterize_lines(lines, grid, self.line_width_m)
        if points:
            mask |= rasterize_points(points, grid, self.point_radius_m)
        return mask

    def _exclusion_points(self, layer: str, box: Any, bounds: tuple[float, float, float, float]) -> list[tuple[float, float]]:
        """Positions of every exclusion point (every part of a multipoint) inside the window (DTM CRS)."""
        layer_sr = arcpy.Describe(layer).spatialReference
        window = box.polygon
        if not same_spatial_reference(layer_sr, self.sr):
            window = window.projectAs(layer_sr)
        try:
            cursor = arcpy.da.SearchCursor(
                layer, ["SHAPE@"], spatial_reference=self.sr,
                spatial_filter=window, spatial_relationship="INTERSECTS",
            )
        except TypeError:  # an ArcGIS Pro build without spatial_filter: read all and filter by position
            cursor = arcpy.da.SearchCursor(layer, ["SHAPE@"], spatial_reference=self.sr)
        x_min, y_min, x_max, y_max = bounds
        result: list[tuple[float, float]] = []
        with cursor:
            for (shape,) in cursor:
                if shape is None:
                    continue
                if self.exclusion_kinds.get(layer) == "Multipoint":
                    members = [point for part in shape for point in ([part] if hasattr(part, "X") else list(part))]
                else:
                    members = [shape.firstPoint]
                for point in members:
                    if point is not None and x_min <= point.X <= x_max and y_min <= point.Y <= y_max:
                        result.append((point.X, point.Y))
        return result

    def _exclusion_lines(self, layer: str, box: Any, bounds: tuple[float, float, float, float]) -> list[Any]:
        """Parts (lists of points) of every exclusion line that touches the window (geometry in the DTM CRS)."""
        layer_sr = arcpy.Describe(layer).spatialReference
        window = box.polygon
        if not same_spatial_reference(layer_sr, self.sr):
            window = window.projectAs(layer_sr)
        try:
            cursor = arcpy.da.SearchCursor(
                layer, ["SHAPE@"], spatial_reference=self.sr,
                spatial_filter=window, spatial_relationship="INTERSECTS",
            )
        except TypeError:
            cursor = arcpy.da.SearchCursor(layer, ["SHAPE@"], spatial_reference=self.sr)
        x_min, y_min, x_max, y_max = bounds
        result: list[Any] = []
        with cursor:
            for (shape,) in cursor:
                if shape is None:
                    continue
                shape_box = shape.extent
                if shape_box.XMax < x_min or shape_box.XMin > x_max or shape_box.YMax < y_min or shape_box.YMin > y_max:
                    continue
                for part in shape:
                    points = [(point.X, point.Y) for point in part if point is not None]
                    if points:
                        result.append(points)
        return result

    def _exclusion_polygons(self, layer: str, box: Any, bounds: tuple[float, float, float, float]) -> list[Any]:
        """Rings of every exclusion polygon that touches the window (geometry in the DTM CRS)."""
        layer_sr = arcpy.Describe(layer).spatialReference
        window = box.polygon
        if not same_spatial_reference(layer_sr, self.sr):
            window = window.projectAs(layer_sr)
        try:
            cursor = arcpy.da.SearchCursor(
                layer, ["SHAPE@"], spatial_reference=self.sr,
                spatial_filter=window, spatial_relationship="INTERSECTS",
            )
        except TypeError:  # an ArcGIS Pro build without spatial_filter: read all and filter by extent
            cursor = arcpy.da.SearchCursor(layer, ["SHAPE@"], spatial_reference=self.sr)
        x_min, y_min, x_max, y_max = bounds
        result: list[Any] = []
        with cursor:
            for (shape,) in cursor:
                if shape is None:
                    continue
                shape_box = shape.extent
                if shape_box.XMax < x_min or shape_box.XMin > x_max or shape_box.YMax < y_min or shape_box.YMin > y_max:
                    continue
                rings: list[list[tuple[float, float]]] = []
                for part in shape:
                    ring: list[tuple[float, float]] = []
                    for point in part:
                        if point is None:  # separates the interior rings (holes) inside a part
                            if len(ring) >= 3:
                                rings.append(ring)
                            ring = []
                        else:
                            ring.append((point.X, point.Y))
                    if len(ring) >= 3:
                        rings.append(ring)
                if rings:
                    result.append(rings)
        return result

    def _lower_left(self, row1: int, col0: int) -> Any:
        extent = self.info["extent"]
        return arcpy.Point(extent.XMin + col0 * self.cell_w, extent.YMax - row1 * self.cell_h)

    def read(self, row0: int, row1: int, col0: int, col1: int):  # type: ignore[no-untyped-def]
        ncols = col1 - col0
        nrows = row1 - row0
        lower_left = self._lower_left(row1, col0)
        dtm, dtm_valid = _read_window(self.dtm_source, lower_left, ncols, nrows)
        if self.z_scale != 1.0:
            dtm *= np.float32(self.z_scale)
        if not self.has_dsm:
            return dtm, dtm_valid, None, None
        source = self.dsm_path
        if not self.dsm_aligned:
            window_extent = arcpy.Extent(
                lower_left.X, lower_left.Y, lower_left.X + ncols * self.cell_w, lower_left.Y + nrows * self.cell_h
            )
            source = os.path.join(self.scratch, "hlz_dsm_tile")
            with arcpy.EnvManager(
                outputCoordinateSystem=self.sr,
                snapRaster=self.dtm_source,
                extent=window_extent,
                cellSize=self.cell_w,
                overwriteOutput=True,
            ):
                if self.dsm_same_sr:
                    arcpy.management.Resample(self.dsm_path, source, f"{self.cell_w} {self.cell_h}", "BILINEAR")
                else:
                    arcpy.management.ProjectRaster(
                        self.dsm_path, source, self.sr, "BILINEAR", f"{self.cell_w} {self.cell_h}"
                    )
        dsm, dsm_valid = _read_window(source, lower_left, ncols, nrows)
        if self.z_scale != 1.0:
            dsm *= np.float32(self.z_scale)
        return dtm, dtm_valid, dsm, dsm_valid & dtm_valid

    def aoi_mask(self, row0: int, row1: int, col0: int, col1: int) -> np.ndarray | None:
        polygon = self._polygon_mask(row0, row1, col0, col1)
        if self.search_area is None:
            return polygon
        extent = self.info["extent"]
        tile = GridGeometry(extent.XMin + col0 * self.cell_w, extent.YMax - row0 * self.cell_h, self.cell_w, -self.cell_h, col1 - col0, row1 - row0)
        return combine_masks((row1 - row0, col1 - col0), polygon, self.search_area.mask(tile))

    def _polygon_mask(self, row0: int, row1: int, col0: int, col1: int) -> np.ndarray | None:
        if self.polygon is None:
            return None
        extent = self.info["extent"]
        x_min = extent.XMin + col0 * self.cell_w
        x_max = extent.XMin + col1 * self.cell_w
        y_max = extent.YMax - row0 * self.cell_h
        y_min = extent.YMax - row1 * self.cell_h
        box = arcpy.Extent(x_min, y_min, x_max, y_max, spatial_reference=self.sr)
        box_polygon = box.polygon
        if self.polygon.disjoint(box_polygon):
            return np.zeros((row1 - row0, col1 - col0), dtype=bool)
        if self.polygon.contains(box_polygon):
            return None
        clipped = self.polygon.clip(box)
        tile_geometry = GridGeometry(x_min, y_max, self.cell_w, -self.cell_h, col1 - col0, row1 - row0)
        return rasterize_polygon(clipped, tile_geometry)


def _is_multiple(value: float, step: float) -> bool:
    ratio = value / step
    return abs(ratio - round(ratio)) < 1.0e-3


# ---------------------------------------------------------------------------
# Writing outputs
# ---------------------------------------------------------------------------

FOOTPRINT_FIELDS: list[tuple[str, str, str, int, str]] = [
    # (field, type, alias, length, candidate attribute or computed key)
    ("Rank", "SHORT", "Rank", 0, "rank"),
    ("HLZ_ID", "TEXT", "HLZ ID", 12, "candidate_id"),
    ("Rating", "TEXT", "Rating", 20, "rating"),
    ("Score", "DOUBLE", "Score (0-100)", 0, "score"),
    ("Status", "TEXT", "Profile status", 60, "status"),
    ("Confidence", "TEXT", "Data confidence", 12, "confidence"),
    ("MGRS", "TEXT", "MGRS (1 m)", 24, "mgrs"),
    ("Latitude", "DOUBLE", "Latitude (WGS84)", 0, "latitude"),
    ("Longitude", "DOUBLE", "Longitude (WGS84)", 0, "longitude"),
    ("Elev_m", "DOUBLE", "Elevation (m)", 0, "elevation_m"),
    ("Slope_deg", "DOUBLE", "Slope (deg)", 0, "slope_deg"),
    ("Aspect_deg", "DOUBLE", "Downslope direction (deg)", 0, "aspect_deg"),
    ("Upslope_Only", "TEXT", "Upslope landing only", 3, "upslope_only"),
    ("Rough_m", "DOUBLE", "Roughness (m)", 0, "roughness_m"),
    ("Obst_TDP_m", "DOUBLE", "Max obstacle in TDP (m)", 0, "max_tdp_obstacle_m"),
    ("Obst_Ring_m", "DOUBLE", "Max obstacle in cleared ring (m)", 0, "max_ring_obstacle_m"),
    ("Excl_Dist_m", "DOUBLE", "Distance to exclusion area (m)", 0, "excluded_distance_m"),
    ("Obst_Dist_m", "DOUBLE", "Distance to nearest listed obstacle (m)", 0, "nearest_obstacle_m"),
    ("Obst_Name", "TEXT", "Nearest listed obstacle", 120, "nearest_obstacle_text"),
    ("Dist_Ctr_m", "DOUBLE", "Distance from search centre (m)", 0, "search_distance_m"),
    ("Brg_Ctr", "DOUBLE", "Bearing from search centre (deg T)", 0, "search_bearing_deg"),
    ("Appr_From", "DOUBLE", "Best approach from (deg T)", 0, "best_approach_azimuth_deg"),
    ("Land_Hdg", "DOUBLE", "Landing heading (deg T)", 0, "landing_heading_deg"),
    ("Appr_Angle", "DOUBLE", "Approach obstacle angle (deg)", 0, "best_approach_angle_deg"),
    ("Clear_Sect", "SHORT", "Clear sectors", 0, "clear_sector_count"),
    ("Assess_Sect", "SHORT", "Assessed sectors", 0, "assessed_sector_count"),
    ("Opposing", "TEXT", "Opposing corridors", 3, "opposing_corridors"),
    ("Appr_Basis", "TEXT", "Approach checked against", 16, "approach_basis"),
    ("DA_ft", "DOUBLE", "Density altitude (ft)", 0, "density_altitude_ft"),
    ("Limiting", "TEXT", "Main limiting factor", 160, "limiting_factor"),
    ("Notes", "TEXT", "Notes", 400, "notes"),
]


def _quietly(function: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Run a cosmetic geoprocessing step; failures only go to the log."""
    try:
        function(*args, **kwargs)
    except Exception as error:  # noqa: BLE001
        LOGGER.debug("Optional step %s failed: %s", getattr(function, "__name__", function), error)


def _field_value(candidate: Candidate, key: str, field_type: str, length: int) -> Any:
    value = getattr(candidate, key)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if value is None:
        return None
    if field_type == "TEXT":
        return str(value)[:length]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class ArcpyWriter:
    """Writes rasters and feature classes into a file geodatabase."""

    def __init__(self, gdb: str, spatial_reference: Any, tiles_folder: str):
        self.gdb = gdb
        self.sr = spatial_reference
        self.tiles_folder = Path(tiles_folder)
        self.specs: dict[str, tuple[str, float, str]] = {}
        self.grid: GridGeometry | None = None

    # -- rasters -----------------------------------------------------------
    def begin_rasters(self, grid: GridGeometry, specs: list[tuple[str, str, float, str]], resume: bool) -> None:
        self.grid = grid
        self.tiles_folder.mkdir(parents=True, exist_ok=True)
        self.specs = {name: (dtype, nodata, label) for name, dtype, nodata, label in specs}

    def write_raster_tile(self, name: str, array: np.ndarray, tile_geometry: GridGeometry, tile: Any) -> None:
        dtype, nodata, _ = self.specs[name]
        output = np.asarray(array).copy()
        if np.issubdtype(output.dtype, np.floating):
            output[~np.isfinite(output)] = nodata
        output = output.astype(dtype)
        lower_left = arcpy.Point(tile_geometry.x_min, tile_geometry.y_min)
        raster = arcpy.NumPyArrayToRaster(
            output,
            lower_left,
            abs(tile_geometry.pixel_width),
            abs(tile_geometry.pixel_height),
            nodata,
        )
        path = str(self.tiles_folder / f"{name}_{tile.name}.tif")
        with arcpy.EnvManager(overwriteOutput=True, compression="LZ77"):
            raster.save(path)
        arcpy.management.DefineProjection(path, self.sr)

    def finish_rasters(self) -> dict[str, str]:
        """Mosaic the tile rasters of each output into the geodatabase."""
        paths: dict[str, str] = {}
        pixel_types = {"uint8": "8_BIT_UNSIGNED", "float32": "32_BIT_FLOAT"}
        cell = abs(self.grid.pixel_width) if self.grid else None
        for name, (dtype, nodata, label) in self.specs.items():
            inputs = sorted(str(path) for path in self.tiles_folder.glob(f"{name}_tile_*.tif"))
            if not inputs:
                continue
            output = os.path.join(self.gdb, name)
            if arcpy.Exists(output):
                arcpy.management.Delete(output)
            LOGGER.info("Mosaicking %d tile(s) into %s", len(inputs), name)
            with arcpy.EnvManager(overwriteOutput=True, compression="LZ77", pyramid="NONE"):
                arcpy.management.MosaicToNewRaster(
                    inputs, self.gdb, name, self.sr, pixel_types[dtype], cell, 1, "FIRST", "FIRST"
                )
            nodata_text = f"1 {int(nodata)}" if dtype == "uint8" else f"1 {nodata}"
            _quietly(arcpy.management.SetRasterProperties, output, nodata=nodata_text)
            _quietly(arcpy.management.CalculateStatistics, output)
            if dtype == "uint8":
                _quietly(arcpy.management.BuildRasterAttributeTable, output, "Overwrite")
            _quietly(arcpy.management.BuildPyramids, output)
            LOGGER.debug("Raster written: %s (%s)", output, label)
            paths[name] = output
        return paths

    def write_search_area(self, search: SearchArea) -> dict[str, str]:
        """The search circles as a polygon layer."""
        path = self._create("HLZ_Search_Area", "POLYGON", [["Centre_No", "SHORT", "Centre number", None], ["Radius_m", "DOUBLE", "Radius (m)", None]])
        with arcpy.da.InsertCursor(path, ["SHAPE@", "Centre_No", "Radius_m"]) as cursor:
            for number, (x, y) in enumerate(search.centres, start=1):
                cursor.insertRow([self._polygon(circle_ring(x, y, search.radius_m, 180)), number, search.radius_m])
        return {"search_area": path}

    def write_overview(self, overview: OverviewResult) -> dict[str, str]:
        """The terrain overview grid as a polygon layer (v0.7.0)."""
        fields = [[name, kind, alias, length if kind == "TEXT" else None] for name, kind, alias, length in OVERVIEW_FIELDS]
        path = self._create("HLZ_Overview_Grid", "POLYGON", fields)
        names = [name for name, *_ in OVERVIEW_FIELDS]
        with arcpy.da.InsertCursor(path, ["SHAPE@", *names]) as cursor:
            for _, ring, attributes in overview.rows():
                cursor.insertRow([self._polygon(ring), *[attributes.get(name) for name in names]])
        return {"overview_grid": path}

    # -- coordinates ---------------------------------------------------------
    def to_latlon(self, xs: list[float], ys: list[float]) -> tuple[list[float], list[float]]:
        wgs84 = arcpy.SpatialReference(4326)
        transformation = ""
        try:
            extent = arcpy.Extent(min(xs), min(ys), max(xs), max(ys), spatial_reference=self.sr)
            transformations = arcpy.ListTransformations(self.sr, wgs84, extent)
            if transformations:
                transformation = transformations[0]
                LOGGER.info("Datum transformation used for lat/long: %s", transformation)
        except Exception:  # noqa: BLE001
            transformation = ""
        latitudes: list[float] = []
        longitudes: list[float] = []
        for x, y in zip(xs, ys):
            point = arcpy.PointGeometry(arcpy.Point(x, y), self.sr)
            projected = point.projectAs(wgs84, transformation) if transformation else point.projectAs(wgs84)
            latitudes.append(projected.firstPoint.Y)
            longitudes.append(projected.firstPoint.X)
        return latitudes, longitudes

    # -- vectors -----------------------------------------------------------------
    def _polygon(self, ring: list[tuple[float, float]]) -> Any:
        return arcpy.Polygon(arcpy.Array([arcpy.Point(x, y) for x, y in ring]), self.sr)

    def _polyline(self, points: list[tuple[float, float]]) -> Any:
        return arcpy.Polyline(arcpy.Array([arcpy.Point(x, y) for x, y in points]), self.sr)

    def _create(self, name: str, geometry_type: str, fields: list[list[Any]]) -> str:
        path = os.path.join(self.gdb, name)
        if arcpy.Exists(path):
            arcpy.management.Delete(path)
        arcpy.management.CreateFeatureclass(self.gdb, name, geometry_type, spatial_reference=self.sr)
        arcpy.management.AddFields(path, fields)
        return path

    def write_vectors(
        self, candidates: list[Candidate], aircraft: AircraftProfile, top_sectors: int
    ) -> dict[str, str]:
        footprint_fields = [
            [name, field_type, alias, length if field_type == "TEXT" else None]
            for name, field_type, alias, length, _ in FOOTPRINT_FIELDS
        ]
        footprints = self._create("HLZ_Footprints", "POLYGON", footprint_fields)
        rings = self._create(
            "HLZ_Cleared_Ring",
            "POLYGON",
            [["HLZ_ID", "TEXT", "HLZ ID", 12], ["Rating", "TEXT", "Rating", 20], ["Rank", "SHORT", "Rank", None]],
        )
        approaches = self._create(
            "HLZ_Best_Approach",
            "POLYLINE",
            [
                ["HLZ_ID", "TEXT", "HLZ ID", 12],
                ["Rank", "SHORT", "Rank", None],
                ["Appr_From", "DOUBLE", "Approach from (deg T)", None],
                ["Land_Hdg", "DOUBLE", "Landing heading (deg T)", None],
                ["Appr_Angle", "DOUBLE", "Obstacle angle (deg)", None],
                ["Label", "TEXT", "Label", 60],
            ],
        )
        sectors = self._create(
            "HLZ_Approach_Sectors",
            "POLYGON",
            [
                ["HLZ_ID", "TEXT", "HLZ ID", 12],
                ["From_deg", "DOUBLE", "Sector from (deg T)", None],
                ["Class", "TEXT", "Sector class", 12],
                ["Angle_deg", "DOUBLE", "Obstacle angle (deg)", None],
                ["Coverage", "DOUBLE", "Data coverage (0-1)", None],
            ],
        )
        names = [name for name, *_ in FOOTPRINT_FIELDS]
        with arcpy.da.InsertCursor(footprints, ["SHAPE@", *names]) as cursor:
            for candidate in candidates:
                values = [
                    _field_value(candidate, key, field_type, length)
                    for _, field_type, _, length, key in FOOTPRINT_FIELDS
                ]
                cursor.insertRow(
                    [self._polygon(circle_ring(candidate.x, candidate.y, aircraft.tdp_radius_m)), *values]
                )
        if aircraft.cleared_ring_m > 0:
            with arcpy.da.InsertCursor(rings, ["SHAPE@", "HLZ_ID", "Rating", "Rank"]) as cursor:
                for candidate in candidates:
                    outer = circle_ring(candidate.x, candidate.y, aircraft.analysis_radius_m)
                    cursor.insertRow([self._polygon(outer), candidate.candidate_id, candidate.rating, candidate.rank])
        with arcpy.da.InsertCursor(
            approaches, ["SHAPE@", "HLZ_ID", "Rank", "Appr_From", "Land_Hdg", "Appr_Angle", "Label"]
        ) as cursor:
            for candidate in candidates:
                if candidate.best_approach_azimuth_deg is None:
                    continue
                line = approach_line(
                    candidate.x, candidate.y, candidate.best_approach_azimuth_deg, aircraft.approach_range_m
                )
                label = (
                    f"{candidate.candidate_id} land {int(round(candidate.landing_heading_deg or 0)) % 360:03d}"
                    f"\u00b0 ({candidate.best_approach_angle_deg:.1f}\u00b0)"
                )
                cursor.insertRow(
                    [
                        self._polyline(line),
                        candidate.candidate_id,
                        candidate.rank,
                        candidate.best_approach_azimuth_deg,
                        candidate.landing_heading_deg,
                        candidate.best_approach_angle_deg,
                        label,
                    ]
                )
        with arcpy.da.InsertCursor(
            sectors, ["SHAPE@", "HLZ_ID", "From_deg", "Class", "Angle_deg", "Coverage"]
        ) as cursor:
            for candidate in candidates:
                if candidate.rank > top_sectors:
                    continue
                for sector in candidate.sectors:
                    ring = sector_ring(
                        candidate.x,
                        candidate.y,
                        sector.azimuth_deg,
                        aircraft.corridor_half_width_deg,
                        aircraft.approach_range_m,
                    )
                    angle = sector.worst_angle_deg if math.isfinite(sector.worst_angle_deg) else None
                    cursor.insertRow(
                        [
                            self._polygon(ring),
                            candidate.candidate_id,
                            sector.azimuth_deg,
                            sector.classification,
                            angle,
                            sector.coverage_fraction,
                        ]
                    )
        return {
            "footprints": footprints,
            "cleared_ring": rings,
            "best_approach": approaches,
            "approach_sectors": sectors,
        }


# ---------------------------------------------------------------------------
# Map presentation
# ---------------------------------------------------------------------------


def _rgb(colour: tuple[int, ...], alpha: int = 100) -> dict[str, list[int]]:
    red, green, blue = colour[:3]
    return {"RGB": [red, green, blue, alpha]}


def _style_unique(layer: Any, field: str, colours: dict[str, tuple[int, int, int]], fill_alpha: int, outline_width: float) -> None:
    symbology = layer.symbology
    if not hasattr(symbology, "renderer"):
        return
    symbology.updateRenderer("UniqueValueRenderer")
    symbology.renderer.fields = [field]
    for group in symbology.renderer.groups:
        for item in group.items:
            value = str(item.values[0][0])
            colour = colours.get(value, (158, 158, 158))
            item.symbol.color = _rgb(colour, fill_alpha)
            try:
                item.symbol.outlineColor = _rgb(colour, 100)
                item.symbol.outlineWidth = outline_width
            except Exception:  # noqa: BLE001 - line symbols have no outline
                item.symbol.width = outline_width
            item.label = value
    layer.symbology = symbology


OVERVIEW_PASS_RGB = {  # v0.7.0: share of an overview cell that passes the ground test
    "High": (56, 142, 60),
    "Medium": (251, 192, 45),
    "Low": (239, 108, 0),
    "None": (158, 158, 158),
}


def _label(layer: Any, expression: str) -> None:
    label_classes = layer.listLabelClasses()
    if not label_classes:
        return
    label_class = label_classes[0]
    label_class.expressionEngine = "Arcade"
    label_class.expression = expression
    label_class.visible = True
    layer.showLabels = True


def add_results_to_map(
    outputs: dict[str, str], run_name: str, warn: Callable[[str], None]
) -> list[str]:
    """Add styled layers to the active map. Returns the layer names added."""
    try:
        project = arcpy.mp.ArcGISProject("CURRENT")
    except Exception:  # noqa: BLE001 - not running inside ArcGIS Pro
        warn("Not running inside an open ArcGIS Pro project; layers were not added to a map.")
        return []
    target_map = project.activeMap
    if target_map is None:
        maps = project.listMaps()
        target_map = maps[0] if maps else None
    if target_map is None:
        warn("No map is open; open a map view and re-run, or add the geodatabase layers manually.")
        return []

    group = None
    try:
        group = target_map.createGroupLayer(f"HLZ results - {run_name}")
    except Exception:  # noqa: BLE001 - older Pro releases have no createGroupLayer
        group = None

    def add(path: str) -> Any:
        return target_map.addDataFromPath(path)

    def move_into_group(layer: Any) -> None:
        """Copy a fully styled layer into the group, then remove the original."""
        if group is None:
            return
        try:
            target_map.addLayerToGroup(group, layer, "TOP")
            target_map.removeLayer(layer)
        except Exception:  # noqa: BLE001 - keep the ungrouped layer
            pass

    added: list[str] = []
    # Added bottom-to-top: each new layer is placed above the previous one.
    order = [
        ("overview_grid", True),
        ("slope", False),
        ("roughness", False),
        ("obstacle", False),
        ("ground_class", True),
        ("approach_sectors", False),
        ("search_area", True),
        ("cleared_ring", True),
        ("best_approach", True),
        ("footprints", True),
    ]
    for key, visible in order:
        path = outputs.get(key)
        if not path:
            continue
        try:
            layer = add(path)
        except Exception as error:  # noqa: BLE001
            warn(f"Could not add {os.path.basename(path)} to the map: {error}")
            continue
        try:
            if key == "footprints":
                layer.name = "HLZ candidates (rating)"
                _style_unique(layer, "Rating", {k: v for k, v in RATING_RGB.items()}, 55, 2.0)
                _label(layer, '$feature.HLZ_ID + " (" + Text($feature.Score, "0") + ")"')
            elif key == "search_area":
                layer.name = "HLZ search area"
                symbology = layer.symbology
                symbology.renderer.symbol.color = {"RGB": [0, 0, 0, 0]}
                symbology.renderer.symbol.outlineColor = {"RGB": [13, 71, 161, 100]}
                symbology.renderer.symbol.outlineWidth = 2.5
                layer.symbology = symbology
            elif key == "cleared_ring":
                layer.name = "HLZ cleared ring"
                _style_unique(layer, "Rating", {k: v for k, v in RATING_RGB.items()}, 0, 1.0)
            elif key == "best_approach":
                layer.name = "HLZ best approach (line points to LZ)"
                symbology = layer.symbology
                symbology.renderer.symbol.color = {"RGB": [13, 71, 161, 100]}
                symbology.renderer.symbol.width = 2.0
                for gallery_name in ("Arrow at End", "Arrow Line", "Arrow Right Middle"):
                    try:
                        symbology.renderer.symbol.applySymbolFromGallery(gallery_name)
                        symbology.renderer.symbol.color = {"RGB": [13, 71, 161, 100]}
                        symbology.renderer.symbol.width = 2.0
                        break
                    except Exception:  # noqa: BLE001
                        continue
                layer.symbology = symbology
                _label(layer, "$feature.Label")
            elif key == "approach_sectors":
                layer.name = "HLZ approach sectors (top candidates)"
                _style_unique(layer, "Class", SECTOR_RGB, 35, 0.4)
                layer.transparency = 40
            elif key == "ground_class":
                layer.name = "Ground test (green any heading / amber upslope)"
                symbology = layer.symbology
                if hasattr(symbology, "colorizer"):
                    symbology.updateColorizer("RasterUniqueValueColorizer")
                    symbology.colorizer.field = "Value"
                    for group_item in symbology.colorizer.groups:
                        for item in group_item.items:
                            value = str(item.values[0])
                            label, colour = GROUND_CLASS_STYLE.get(value, (value, (0, 0, 0, 0)))
                            item.label = label
                            item.color = {"RGB": list(colour)}
                    layer.symbology = symbology
                layer.transparency = 35
            elif key == "overview_grid":
                layer.name = "HLZ overview grid (area passing the ground test)"
                _style_unique(layer, "Pass_Class", OVERVIEW_PASS_RGB, 55, 0.4)
                layer.transparency = 25
            elif key == "slope":
                layer.name = "Slope (deg)"
            elif key == "roughness":
                layer.name = "Roughness (m)"
            elif key == "obstacle":
                layer.name = "Max obstacle height in LZ + ring (m)"
            layer.visible = visible
        except Exception as error:  # noqa: BLE001 - styling is cosmetic
            warn(f"Layer added but styling failed for {key}: {error}")
        added.append(layer.name)
        move_into_group(layer)

    try:
        view = project.activeView
        if view is not None and hasattr(view, "camera") and outputs.get("footprints"):
            extent = arcpy.Describe(outputs["footprints"]).extent
            if extent and extent.width > 0:
                view.camera.setExtent(extent)
                view.camera.scale *= 1.3
    except Exception:  # noqa: BLE001 - zoom is cosmetic
        pass
    return added


def open_file(path: str) -> None:
    try:
        os.startfile(path)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        LOGGER.warning("Could not open %s automatically", Path(path).name)


# ---------------------------------------------------------------------------
# Data folder audit (v0.6.6): formats the standard library cannot read
# ---------------------------------------------------------------------------


def _audit_crs(spatial_reference: Any):  # type: ignore[no-untyped-def]
    from .data_root import CrsInfo, _normalise_unit, _utm_from_name, utm_from_epsg

    info = CrsInfo(source="ArcGIS")
    if spatial_reference is None or not getattr(spatial_reference, "name", "") or spatial_reference.name == "Unknown":
        return info
    info.name = str(spatial_reference.name).replace("_", " ")
    info.kind = {"Projected": "projected", "Geographic": "geographic"}.get(str(spatial_reference.type), "unknown")
    code = int(getattr(spatial_reference, "factoryCode", 0) or 0)
    info.epsg = code or None
    if info.kind == "projected":
        info.unit = _normalise_unit(str(getattr(spatial_reference, "linearUnitName", "") or ""))
    elif info.kind == "geographic":
        info.unit = "degree"
    utm = utm_from_epsg(code)
    if utm:
        info.utm_zone, info.utm_north = utm[0], utm[1]
    _utm_from_name(info)
    if code in (3857, 102100, 102113, 900913):
        info.web_mercator = True
    vcs = getattr(spatial_reference, "VCS", None)
    if vcs is not None:
        info.vertical = str(getattr(vcs, "name", "") or "")
        info.vertical_unit = _normalise_unit(str(getattr(vcs, "linearUnitName", "") or ""))
    return info


def _audit_dataset(path: str, description: Any, root_hint: str = ""):  # type: ignore[no-untyped-def]
    from .data_root import Dataset

    data_type = str(getattr(description, "dataType", ""))
    name = str(getattr(description, "name", Path(path).name))
    dataset = Dataset(path=path, rel="", name=name, folder_key=None, kind="other", fmt=data_type)
    extent = getattr(description, "extent", None)
    if extent is not None and extent.XMin is not None and not math.isnan(float(extent.XMin)):
        dataset.extent = (float(extent.XMin), float(extent.YMin), float(extent.XMax), float(extent.YMax))
    dataset.crs = _audit_crs(getattr(description, "spatialReference", None))
    if data_type in ("RasterDataset", "MosaicDataset", "RasterBand"):
        dataset.kind = "raster"
        dataset.fmt = {"RasterDataset": "Raster dataset", "MosaicDataset": "Mosaic dataset"}.get(data_type, data_type)
        try:
            raster = arcpy.Raster(path)
            dataset.width, dataset.height = int(raster.width), int(raster.height)
            dataset.bands = int(raster.bandCount)
            dataset.cell_w, dataset.cell_h = float(raster.meanCellWidth), float(raster.meanCellHeight)
            dataset.dtype = {"F32": "float32", "F64": "float64", "S16": "int16", "U16": "uint16", "S32": "int32",
                             "U32": "uint32", "U8": "uint8", "S8": "int8"}.get(str(raster.pixelType), str(raster.pixelType))
            dataset.nodata = None if raster.noDataValue is None else str(raster.noDataValue)
            try:
                minimum = float(arcpy.management.GetRasterProperties(path, "MINIMUM")[0])
                maximum = float(arcpy.management.GetRasterProperties(path, "MAXIMUM")[0])
                mean = float(arcpy.management.GetRasterProperties(path, "MEAN")[0])
                dataset.stats = {"min": minimum, "max": maximum, "mean": mean}
            except Exception:  # noqa: BLE001 - no statistics calculated yet
                pass
        except Exception as error:  # noqa: BLE001
            dataset.note = f"Raster properties could not be read: {error}"
    elif data_type in ("FeatureClass", "ShapeFile", "Layer"):
        dataset.kind = "vector"
        dataset.fmt = "Feature class" if data_type == "FeatureClass" else data_type
        shape = str(getattr(description, "shapeType", ""))
        dataset.geometry = {"Polygon": "Polygon", "Polyline": "Line", "Point": "Point", "Multipoint": "Point",
                            "MultiPatch": "MultiPatch"}.get(shape, shape or "Unknown")
        try:
            dataset.feature_count = int(arcpy.management.GetCount(path)[0])
        except Exception:  # noqa: BLE001
            dataset.feature_count = None
    try:
        dataset.size_bytes = Path(path).stat().st_size if Path(path).is_file() else 0
    except OSError:
        dataset.size_bytes = 0
    return dataset


def audit_inspector(path: Path):  # type: ignore[no-untyped-def]
    """``hlzcore.data_root`` inspector: datasets inside a geodatabase, or one raster ArcGIS can read.

    Returns None when ArcGIS cannot describe the item (the audit then reports it as not checked).
    """
    text = str(path)
    if path.is_dir() and path.suffix.lower() in (".gdb", ".sde"):
        found = []
        for folder, _names, items in arcpy.da.Walk(text, datatype=["FeatureClass", "RasterDataset", "MosaicDataset"]):
            for item in items:
                full = os.path.join(folder, item)
                try:
                    found.append(_audit_dataset(full, arcpy.Describe(full)))
                except Exception:  # noqa: BLE001 - skip unreadable items
                    continue
        return found
    try:
        description = arcpy.Describe(text)
    except Exception:  # noqa: BLE001
        return None
    dataset = _audit_dataset(text, description)
    return [dataset] if dataset.kind in ("raster", "vector") else None

