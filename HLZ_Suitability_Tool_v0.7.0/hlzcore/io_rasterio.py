# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Rasterio-based IO adapter for the optional command-line interface.

Not used by the ArcGIS Pro toolbox. Requires ``rasterio`` (install it only in
a dedicated Conda environment, never in ``base`` or ``arcgispro-py3``).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from . import COPYRIGHT, LICENSE_ID, __version__
from .geometry_utils import approach_line, circle_ring, sector_ring
from .masks import rasterize_lines, rasterize_points, rasterize_polygons
from .overview import OVERVIEW_FIELDS, OverviewResult
from .search import SearchArea, intersect_windows
from .models import AircraftProfile, Candidate, GridGeometry
from .obstacles import DEFAULT_OBSTACLE_BUFFER_M, DEFAULT_OBSTACLE_HEIGHT_M, ObstacleError, load_obstacle_files
from .pipeline import AnalysisError
from .report import CSV_COLUMNS

FEET_TO_METRES = 0.3048


def _geojson_geometries(path: str | Path, kinds: tuple[str, ...]) -> list[dict[str, Any]]:
    """Geometries of the given types from a GeoJSON file (any nesting)."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    stack = [document]
    found: list[dict[str, Any]] = []
    while stack:
        item = stack.pop()
        kind = item.get("type")
        if kind == "FeatureCollection":
            stack.extend(item.get("features", []))
        elif kind == "Feature":
            if item.get("geometry"):
                stack.append(item["geometry"])
        elif kind == "GeometryCollection":
            stack.extend(item.get("geometries", []))
        elif kind in kinds:
            found.append(item)
    return found


def _geojson_polygons(path: str | Path) -> list[dict[str, Any]]:
    """Polygon and MultiPolygon geometries from a GeoJSON file (any nesting)."""
    return _geojson_geometries(path, ("Polygon", "MultiPolygon"))


def _geojson_points(path: str | Path) -> list[dict[str, Any]]:
    """Point and MultiPoint geometries of a GeoJSON file (v0.7.0)."""
    return _geojson_geometries(path, ("Point", "MultiPoint"))


def _geojson_lines(path: str | Path) -> list[dict[str, Any]]:
    """LineString and MultiLineString geometries from a GeoJSON file (any nesting)."""
    return _geojson_geometries(path, ("LineString", "MultiLineString"))


class RasterioTileReader:
    """``TileReader`` that reads windows straight from disk (any raster size).

    A DSM on a different grid or CRS is aligned on the fly through a
    ``WarpedVRT`` (bilinear), so no full-size intermediate file is created.
    """

    def __init__(
        self,
        dtm_path: str | Path,
        dsm_path: str | Path | None,
        z_units: str = "METERS",
        aoi_bbox: tuple[float, float, float, float] | None = None,
        exclude_path: "str | Path | list[str | Path] | None" = None,
        exclude_crs: str = "EPSG:4326",
        search_latlon: list[tuple[float, float]] | None = None,
        search_radius_m: float | None = None,
        line_width_m: float = 10.0,
        point_radius_m: float = 10.0,
        obstacle_paths: "str | Path | list[str | Path] | None" = None,
        obstacle_height_field: str | None = None,
        obstacle_height_units: str = "Meters",
        obstacle_default_height_m: float = DEFAULT_OBSTACLE_HEIGHT_M,
        obstacle_buffer_m: float = DEFAULT_OBSTACLE_BUFFER_M,
    ):
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.vrt import WarpedVRT
        from rasterio.windows import Window, from_bounds

        self.notes: list[str] = []
        self.z_scale = FEET_TO_METRES if z_units.upper().startswith("F") else 1.0
        self.dtm_dataset = rasterio.open(dtm_path)
        source = self.dtm_dataset
        if source.count != 1:
            raise AnalysisError("DTM must contain exactly one band")
        if source.crs is None or not source.crs.is_projected:
            raise AnalysisError("DTM must use a projected CRS in metres (e.g. UTM). Reproject it first.")
        unit = (source.crs.linear_units or "").lower()
        if unit not in {"metre", "meter", "metres", "meters"}:
            raise AnalysisError(f"DTM horizontal units must be metres; found {unit!r}")
        transform = source.transform
        if abs(transform.b) > 1.0e-9 or abs(transform.d) > 1.0e-9:
            raise AnalysisError("Rotated rasters are not supported")
        if not math.isclose(abs(transform.a), abs(transform.e), rel_tol=1.0e-6):
            raise AnalysisError("DTM cells must be square")
        self.crs = source.crs
        self.profile = source.profile.copy()
        self.geometry = GridGeometry(
            x_origin=float(transform.c),
            y_origin=float(transform.f),
            pixel_width=float(transform.a),
            pixel_height=float(transform.e),
            width=int(source.width),
            height=int(source.height),
        )
        self.window = (0, source.height, 0, source.width)
        self.aoi_digest = "none"
        if aoi_bbox is not None:
            win = from_bounds(*aoi_bbox, transform=transform).round_offsets().round_lengths()
            win = win.intersection(Window(0, 0, source.width, source.height))
            self.window = (
                int(win.row_off),
                int(win.row_off + win.height),
                int(win.col_off),
                int(win.col_off + win.width),
            )
            self.aoi_digest = f"bbox:{tuple(round(value, 3) for value in aoi_bbox)}"
        self.crs_description = self.crs.to_string()
        self.dtm_source = str(Path(dtm_path).resolve())
        self.dsm_source = str(Path(dsm_path).resolve()) if dsm_path else None
        self.has_dsm = dsm_path is not None
        self.dsm_dataset = None
        self.dsm_reader = None
        if dsm_path:
            dsm = rasterio.open(dsm_path)
            self.dsm_dataset = dsm
            if dsm.count != 1:
                raise AnalysisError("DSM must contain exactly one band")
            aligned = (
                dsm.crs == self.crs
                and dsm.width == source.width
                and dsm.height == source.height
                and dsm.transform.almost_equals(transform)
            )
            if aligned:
                self.dsm_reader = dsm
            else:
                self.notes.append(
                    "DSM grid differs from the DTM: it is resampled (bilinear) onto the DTM grid on the fly"
                )
                self.dsm_reader = WarpedVRT(
                    dsm,
                    crs=self.crs,
                    transform=transform,
                    width=source.width,
                    height=source.height,
                    resampling=Resampling.bilinear,
                    src_nodata=dsm.nodata,
                    nodata=np.nan,
                    dtype="float32",
                )
        self._Window = Window
        self.search_area = None
        if search_latlon and search_radius_m:
            from rasterio.warp import transform as warp_transform

            xs, ys = warp_transform("EPSG:4326", self.crs, [lon for _, lon in search_latlon], [lat for lat, _ in search_latlon])
            self.search_area = SearchArea(list(zip(xs, ys)), search_radius_m)
            circle_window = self.search_area.window(self.geometry)
            combined = intersect_windows(self.window, circle_window) if circle_window else None
            if combined is None:
                raise AnalysisError("The search circle does not overlap the DTM (or the --aoi box).")
            self.window = combined
            self.aoi_digest = f"{self.aoi_digest}|{self.search_area.digest}"
        self.has_exclusion = False
        self.exclusion_digest = "none"
        self.exclusion_description = "none"
        self._exclusion_polygons: list[Any] = []
        self._exclusion_boxes = np.zeros((0, 4))
        self._exclusion_lines: list[Any] = []
        self._exclusion_line_boxes = np.zeros((0, 4))
        self.line_width_m = float(line_width_m)
        self.point_radius_m = float(point_radius_m)
        self._exclusion_points: list[tuple[float, float]] = []
        exclude_paths = [exclude_path] if isinstance(exclude_path, (str, Path)) else list(exclude_path or [])
        if exclude_paths:
            from rasterio.warp import transform_geom

            boxes = []
            for geometry in [g for path in exclude_paths for g in _geojson_polygons(path)]:
                if str(exclude_crs) != self.crs.to_string():
                    geometry = transform_geom(str(exclude_crs), self.crs, geometry)
                polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
                for rings in polygons:
                    xs = [point[0] for ring in rings for point in ring]
                    ys = [point[1] for ring in rings for point in ring]
                    if len(xs) >= 3:
                        self._exclusion_polygons.append([[(p[0], p[1]) for p in ring] for ring in rings])
                        boxes.append((min(xs), min(ys), max(xs), max(ys)))
            self._exclusion_boxes = np.asarray(boxes, dtype=np.float64).reshape(-1, 4)
            line_boxes = []
            for geometry in [g for path in exclude_paths for g in _geojson_lines(path)]:
                if str(exclude_crs) != self.crs.to_string():
                    geometry = transform_geom(str(exclude_crs), self.crs, geometry)
                parts = [geometry["coordinates"]] if geometry["type"] == "LineString" else geometry["coordinates"]
                for part in parts:
                    if part:
                        xs = [point[0] for point in part]
                        ys = [point[1] for point in part]
                        self._exclusion_lines.append([(p[0], p[1]) for p in part])
                        line_boxes.append((min(xs), min(ys), max(xs), max(ys)))
            self._exclusion_line_boxes = np.asarray(line_boxes, dtype=np.float64).reshape(-1, 4)
            for geometry in [g for path in exclude_paths for g in _geojson_points(path)]:
                if str(exclude_crs) != self.crs.to_string():
                    geometry = transform_geom(str(exclude_crs), self.crs, geometry)
                members = [geometry["coordinates"]] if geometry["type"] == "Point" else geometry["coordinates"]
                for member in members:
                    if member and len(member) >= 2:
                        self._exclusion_points.append((float(member[0]), float(member[1])))
            self._exclusion_point_array = np.asarray(self._exclusion_points, dtype=np.float64).reshape(-1, 2)
            if not self._exclusion_polygons and not self._exclusion_lines and not self._exclusion_points:
                raise AnalysisError(f"{', '.join(Path(p).name for p in exclude_paths)} holds no polygons, lines or points.")
            self.has_exclusion = True
            text = f"{len(self._exclusion_polygons):,} polygons"
            if self._exclusion_lines:
                text += f", {len(self._exclusion_lines):,} lines widened to {self.line_width_m:g} m"
            if self._exclusion_points:
                text += f", {len(self._exclusion_points):,} points with a {self.point_radius_m:g} m radius"
            self.exclusion_description = f"{', '.join(Path(p).name for p in exclude_paths)} ({text})"
            self.exclusion_digest = (
                f"{'|'.join(str(Path(p).resolve()) for p in exclude_paths)}:{len(self._exclusion_polygons)}"
                + (f":lines{len(self._exclusion_lines)}@{self.line_width_m:g}" if self._exclusion_lines else "")
                + (f":points{len(self._exclusion_points)}@{self.point_radius_m:g}" if self._exclusion_points else "")
            )

        # Listed obstacles: FAA DOF (.dat), CSV or GeoJSON (WGS84), projected to the DTM CRS --------------
        self.obstacles = None
        self.obstacle_description = "none"
        self.obstacle_digest = "none"
        obstacle_list = [obstacle_paths] if isinstance(obstacle_paths, (str, Path)) else list(obstacle_paths or [])
        if obstacle_list:
            from rasterio.warp import transform as warp_transform
            from rasterio.warp import transform_bounds

            box = self.geometry
            west, south, east, north = transform_bounds(
                self.crs, "EPSG:4326", box.x_origin, box.y_min, box.x_max, box.y_origin
            )
            margin = 0.05  # about 5 km: obstacles just outside the DTM can still matter for the approaches
            bbox_ll = (west - margin, south - margin, east + margin, north + margin)
            try:
                load = load_obstacle_files(
                    obstacle_list, bbox_ll,
                    lambda lons, lats: tuple(warp_transform("EPSG:4326", self.crs, lons, lats)),  # type: ignore[arg-type]
                    obstacle_buffer_m, abs(box.pixel_width), obstacle_height_field, obstacle_height_units,
                    obstacle_default_height_m,
                )
            except ObstacleError as error:
                raise AnalysisError(str(error)) from error
            self.obstacles = load.obstacles
            self.obstacle_description = load.description
            self.obstacle_digest = load.digest
            self.notes.extend(load.notes)

    def _read(self, dataset, row0: int, row1: int, col0: int, col1: int):  # type: ignore[no-untyped-def]
        window = self._Window(col0, row0, col1 - col0, row1 - row0)
        masked = dataset.read(1, window=window, masked=True)
        values = np.asarray(masked.filled(np.nan), dtype=np.float32)
        valid = ~np.ma.getmaskarray(masked) & np.isfinite(values)
        values *= np.float32(self.z_scale)
        values[~valid] = np.nan
        return values, valid

    def read(self, row0: int, row1: int, col0: int, col1: int):  # type: ignore[no-untyped-def]
        dtm, dtm_valid = self._read(self.dtm_dataset, row0, row1, col0, col1)
        if self.dsm_reader is None:
            return dtm, dtm_valid, None, None
        dsm, dsm_valid = self._read(self.dsm_reader, row0, row1, col0, col1)
        return dtm, dtm_valid, dsm, dsm_valid & dtm_valid

    def exclusion_mask(self, row0: int, row1: int, col0: int, col1: int) -> np.ndarray | None:
        if not self.has_exclusion:
            return None
        geometry = self.geometry
        x_min = geometry.x_origin + col0 * geometry.pixel_width
        x_max = geometry.x_origin + col1 * geometry.pixel_width
        y_max = geometry.y_origin + row0 * geometry.pixel_height
        y_min = geometry.y_origin + row1 * geometry.pixel_height
        boxes = self._exclusion_boxes
        hit = np.nonzero((boxes[:, 2] >= x_min) & (boxes[:, 0] <= x_max) & (boxes[:, 3] >= y_min) & (boxes[:, 1] <= y_max))[0]
        window = GridGeometry(x_min, y_max, geometry.pixel_width, geometry.pixel_height, col1 - col0, row1 - row0)
        mask = rasterize_polygons([self._exclusion_polygons[i] for i in hit], window)
        if self._exclusion_points:
            reach = self.point_radius_m + 2.0 * abs(geometry.pixel_width)
            pts = self._exclusion_point_array
            near = (pts[:, 0] >= x_min - reach) & (pts[:, 0] <= x_max + reach) & (pts[:, 1] >= y_min - reach) & (pts[:, 1] <= y_max + reach)
            if near.any():
                mask |= rasterize_points([tuple(item) for item in pts[near]], window, self.point_radius_m)
        if self._exclusion_lines:
            margin = self.line_width_m / 2.0 + 2.0 * abs(geometry.pixel_width)
            lb = self._exclusion_line_boxes
            line_hit = np.nonzero((lb[:, 2] >= x_min - margin) & (lb[:, 0] <= x_max + margin)
                                  & (lb[:, 3] >= y_min - margin) & (lb[:, 1] <= y_max + margin))[0]
            if line_hit.size:
                mask |= rasterize_lines([self._exclusion_lines[i] for i in line_hit], window, self.line_width_m)
        return mask

    def aoi_mask(self, row0: int, row1: int, col0: int, col1: int) -> np.ndarray | None:
        if self.search_area is not None:
            tile = GridGeometry(
                self.geometry.x_origin + col0 * self.geometry.pixel_width, self.geometry.y_origin + row0 * self.geometry.pixel_height,
                self.geometry.pixel_width, self.geometry.pixel_height, col1 - col0, row1 - row0)
            return self.search_area.mask(tile)
        return None  # the analysis window already equals the AOI bounding box

    def close(self) -> None:
        for dataset in (self.dsm_reader, self.dsm_dataset, self.dtm_dataset):
            try:
                if dataset is not None:
                    dataset.close()
            except Exception:  # noqa: BLE001
                pass


class RasterioWriter:
    """Writes GeoTIFF rasters and WGS84 GeoJSON layers."""

    def __init__(self, output_folder: str | Path, crs: Any, raster_profile: dict[str, Any]):
        self.folder = Path(output_folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.crs = crs
        self.profile = raster_profile
        self.grid: GridGeometry | None = None
        self.datasets: dict[str, Any] = {}
        self.paths: dict[str, str] = {}

    def begin_rasters(self, grid: GridGeometry, specs: list[tuple[str, str, float, str]], resume: bool) -> None:
        """Create (or reopen when resuming) one GeoTIFF per raster for the analysis area."""
        import rasterio
        from rasterio.transform import from_origin

        self.grid = grid
        for name, dtype, nodata, label in specs:
            path = self.folder / f"{name}.tif"
            if resume and path.exists():
                dataset = rasterio.open(path, "r+")
            else:
                profile = {
                    "driver": "GTiff",
                    "width": grid.width,
                    "height": grid.height,
                    "count": 1,
                    "dtype": dtype,
                    "nodata": nodata,
                    "crs": self.crs,
                    "transform": from_origin(grid.x_origin, grid.y_origin, grid.pixel_width, abs(grid.pixel_height)),
                    "compress": "deflate",
                    "BIGTIFF": "IF_SAFER",
                    "SPARSE_OK": "TRUE",
                }
                if grid.width >= 256 and grid.height >= 256:
                    profile.update(tiled=True, blockxsize=256, blockysize=256)
                dataset = rasterio.open(path, "w", **profile)
                dataset.set_band_description(1, label)
                dataset.update_tags(
                    SPDX_License_Identifier=LICENSE_ID, Copyright=COPYRIGHT, Tool_Version=__version__
                )
            self.datasets[name] = (dataset, dtype, nodata)
            self.paths[name] = str(path)

    def write_raster_tile(self, name: str, array: np.ndarray, tile_geometry: GridGeometry, tile: Any) -> None:
        from rasterio.windows import Window

        dataset, dtype, nodata = self.datasets[name]
        output = np.asarray(array).copy()
        if np.issubdtype(output.dtype, np.floating):
            output[~np.isfinite(output)] = nodata
        assert self.grid is not None
        col_off = int(round((tile_geometry.x_origin - self.grid.x_origin) / self.grid.pixel_width))
        row_off = int(round((tile_geometry.y_origin - self.grid.y_origin) / self.grid.pixel_height))
        dataset.write(
            output.astype(dtype), 1, window=Window(col_off, row_off, output.shape[1], output.shape[0])
        )

    def finish_rasters(self) -> dict[str, str]:
        for dataset, _, _ in self.datasets.values():
            dataset.close()
        self.datasets.clear()
        return dict(self.paths)

    def to_latlon(self, xs: list[float], ys: list[float]) -> tuple[list[float], list[float]]:
        from rasterio.warp import transform

        longitudes, latitudes = transform(self.crs, "EPSG:4326", xs, ys)
        return list(latitudes), list(longitudes)

    def _ring_to_wgs84(self, ring: list[tuple[float, float]]) -> list[list[float]]:
        from rasterio.warp import transform

        xs = [point[0] for point in ring]
        ys = [point[1] for point in ring]
        longitudes, latitudes = transform(self.crs, "EPSG:4326", xs, ys)
        return [[round(lon, 8), round(lat, 8)] for lon, lat in zip(longitudes, latitudes)]

    def write_vectors(
        self, candidates: list[Candidate], aircraft: AircraftProfile, top_sectors: int
    ) -> dict[str, str]:
        footprints: list[dict[str, Any]] = []
        approaches: list[dict[str, Any]] = []
        sectors: list[dict[str, Any]] = []
        for candidate in candidates:
            data = candidate.to_dict()
            properties = {header: data.get(key) for header, key in CSV_COLUMNS}
            footprints.append(
                {
                    "type": "Feature",
                    "properties": properties,
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            self._ring_to_wgs84(circle_ring(candidate.x, candidate.y, aircraft.tdp_radius_m))
                        ],
                    },
                }
            )
            if candidate.best_approach_azimuth_deg is not None:
                line = approach_line(
                    candidate.x, candidate.y, candidate.best_approach_azimuth_deg, aircraft.approach_range_m
                )
                approaches.append(
                    {
                        "type": "Feature",
                        "properties": {
                            "HLZ_ID": candidate.candidate_id,
                            "Rank": candidate.rank,
                            "Approach_from_deg": candidate.best_approach_azimuth_deg,
                            "Landing_heading_deg": candidate.landing_heading_deg,
                            "Obstacle_angle_deg": candidate.best_approach_angle_deg,
                        },
                        "geometry": {"type": "LineString", "coordinates": self._ring_to_wgs84(line)},
                    }
                )
            if candidate.rank <= top_sectors:
                for sector in candidate.sectors:
                    ring = sector_ring(
                        candidate.x,
                        candidate.y,
                        sector.azimuth_deg,
                        aircraft.corridor_half_width_deg,
                        aircraft.approach_range_m,
                    )
                    sectors.append(
                        {
                            "type": "Feature",
                            "properties": {
                                "HLZ_ID": candidate.candidate_id,
                                "From_deg": sector.azimuth_deg,
                                "Class": sector.classification,
                                "Obstacle_angle_deg": None
                                if not math.isfinite(sector.worst_angle_deg)
                                else round(sector.worst_angle_deg, 2),
                                "Coverage": sector.coverage_fraction,
                            },
                            "geometry": {"type": "Polygon", "coordinates": [self._ring_to_wgs84(ring)]},
                        }
                    )
        outputs: dict[str, str] = {}
        for name, features in (
            ("HLZ_Footprints", footprints),
            ("HLZ_Best_Approach", approaches),
            ("HLZ_Approach_Sectors", sectors),
        ):
            path = self.folder / f"{name}.geojson"
            document = {
                "type": "FeatureCollection",
                "name": name,
                "license": LICENSE_ID,
                "copyright": COPYRIGHT,
                "features": features,
            }
            path.write_text(json.dumps(document, indent=1), encoding="utf-8")
            outputs[name] = str(path)
        return outputs


def _write_overview(self: "RasterioWriter", overview: OverviewResult) -> dict[str, str]:
    """The terrain overview grid as WGS84 GeoJSON polygons (v0.7.0)."""
    features = [
        {
            "type": "Feature",
            "properties": attributes,
            "geometry": {"type": "Polygon", "coordinates": [self._ring_to_wgs84(ring)]},
        }
        for _, ring, attributes in overview.rows()
    ]
    path = self.folder / "HLZ_Overview_Grid.geojson"
    document = {
        "type": "FeatureCollection",
        "name": "HLZ_Overview_Grid",
        "license": LICENSE_ID,
        "copyright": COPYRIGHT,
        "fields": [{"name": name, "alias": alias} for name, _, alias, _ in OVERVIEW_FIELDS],
        "features": features,
    }
    path.write_text(json.dumps(document, indent=1), encoding="utf-8")
    return {"overview_grid": str(path)}


def _write_search_area(self: "RasterioWriter", search: SearchArea) -> dict[str, str]:
    features = [
        {
            "type": "Feature",
            "properties": {"Centre_No": number, "Radius_m": search.radius_m},
            "geometry": {"type": "Polygon", "coordinates": [self._ring_to_wgs84(circle_ring(x, y, search.radius_m, 180))]},
        }
        for number, (x, y) in enumerate(search.centres, start=1)
    ]
    path = self.folder / "HLZ_Search_Area.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "name": "HLZ_Search_Area", "features": features}, indent=1), encoding="utf-8")
    return {"search_area": str(path)}


RasterioWriter.write_search_area = _write_search_area  # type: ignore[attr-defined]
RasterioWriter.write_overview = _write_overview  # type: ignore[attr-defined]
