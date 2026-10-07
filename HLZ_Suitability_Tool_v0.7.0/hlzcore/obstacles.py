# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Listed vertical obstacles: towers, masts, wires and buildings (v0.6.9).

Lidar surface models often miss thin structures such as lattice towers, antennas and wires, so the
terrain analysis alone can show a clear approach where a tower stands in the way. This module reads
obstacle lists that work like the obstacle data of an aeronautical chart and feeds them to the analysis:

* FAA Digital Obstacle File (``.dat``), a fixed-width text file refreshed every 56 days.
* A CSV list with latitude, longitude and a height column.
* GeoJSON (for example an OpenStreetMap export from overpass-turbo), points, lines and polygons.
* Any GIS layer ArcGIS Pro can read (shapefile, GeoPackage, geodatabase), read by ``io_arcpy``.

Every obstacle has a height ABOVE GROUND LEVEL (AGL) in metres. Heights that were missing are replaced by
a default and counted, so the report can say how many are assumed.

How the analysis uses an obstacle:

* Its footprint (the point, line or polygon widened by a horizontal safety buffer) is burned into the
  surface model as ``ground + AGL``, so the touchdown-point and cleared-ring tests see it.
* Because a thin tower can fall between the 5 degree rays of the approach analysis, every obstacle is also
  tested exactly: its angle above the clearance point is compared with every approach sector it lies in.
* The nearest obstacle is reported for every candidate.

The module needs only NumPy and the standard library.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np

from .masks import rasterize_lines, rasterize_polygons
from .models import GridGeometry

FEET_TO_METRES = 0.3048
DEFAULT_OBSTACLE_HEIGHT_M = 30.0
DEFAULT_OBSTACLE_BUFFER_M = 10.0
SAMPLE_STEP_M = 5.0
MAX_MARKERS = 3000
SOURCE_SUFFIXES = (".dat", ".csv", ".txt", ".geojson", ".json")

# Field names tried, in order, when no height field is given (compared in lower case without spaces).
HEIGHT_FIELD_CANDIDATES = (
    "agl", "agl_m", "agl_ft", "aglhgt", "agl_hgt", "height_agl", "hgt_agl", "obst_agl", "overallheightaboveground",
    "overall_height", "struct_ht", "structht", "structure_height", "obstacle_height", "height", "height_m", "height_ft",
    "hgt", "ht", "hgt_m", "hgt_ft", "heightm", "heightft",
)
NAME_FIELD_CANDIDATES = ("name", "obstacle_name", "type", "obst_type", "obstacle_type", "description", "desc", "id", "oas")
LAT_NAMES = ("lat", "latitude", "y", "lat_dd", "latitude_dd")
LON_NAMES = ("lon", "lng", "long", "longitude", "x", "lon_dd", "longitude_dd")


class ObstacleError(ValueError):
    """Raised for an obstacle file that cannot be read."""


@dataclass
class Obstacle:
    """One listed obstacle. ``parts`` hold (x, y) pairs: one point, line parts, or the rings of a polygon."""

    kind: str  # "point", "line" or "polygon"
    parts: list[list[tuple[float, float]]]
    agl_m: float
    name: str = ""
    source: str = ""
    assumed: bool = False  # the height is the default, not a value from the data

    def representative_point(self) -> tuple[float, float]:
        points = [point for part in self.parts for point in part]
        if self.kind == "line":
            line = self.parts[0]
            return line[len(line) // 2]
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    def bounds(self) -> tuple[float, float, float, float]:
        points = [point for part in self.parts for point in part]
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class LoadResult:
    """Obstacles read from one source, in latitude/longitude (x = longitude, y = latitude)."""

    obstacles: list[Obstacle] = field(default_factory=list)
    source: str = ""
    records: int = 0
    skipped: int = 0
    outside: int = 0
    assumed: int = 0
    height_field: str = ""
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Heights
# ---------------------------------------------------------------------------

_HEIGHT_TEXT = re.compile(r"^\s*([-+]?\d+(?:[.,]\d+)?)\s*([a-zA-Z'\"]*)")


def parse_height(value: Any, units: str = "Meters", field_name: str = "") -> float | None:
    """Height above ground in metres from a number or text such as ``30``, ``30 m`` or ``100 ft``.

    A text unit wins; otherwise a field name ending in ``_ft`` means feet; otherwise ``units`` decides.
    Returns None when the value is missing, not a number, or not positive.
    """
    if value is None:
        return None
    feet = units.strip().upper().startswith("F")
    name = field_name.strip().lower()
    if name.endswith("ft"):
        feet = True
    elif name.endswith(("_m", "aglm", "heightm", "hgtm")):
        feet = False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    else:
        match = _HEIGHT_TEXT.match(str(value))
        if not match:
            return None
        number = float(match.group(1).replace(",", "."))
        unit = match.group(2).lower()
        if unit in ("ft", "feet", "foot", "'"):
            feet = True
        elif unit in ("m", "meter", "meters", "metre", "metres"):
            feet = False
    if not math.isfinite(number) or number <= 0.0:
        return None
    return number * FEET_TO_METRES if feet else number


def find_field(names: Iterable[str], wanted: str | None, candidates: Sequence[str]) -> str:
    """The real field name to use: ``wanted`` if present (any case), otherwise the first known candidate."""
    lookup = {re.sub(r"[\s]+", "", str(name)).lower(): str(name) for name in names}
    if wanted:
        key = re.sub(r"[\s]+", "", wanted).lower()
        if key in lookup:
            return lookup[key]
        return ""
    for candidate in candidates:
        if candidate in lookup:
            return lookup[candidate]
    return ""


# ---------------------------------------------------------------------------
# Readers (latitude / longitude)
# ---------------------------------------------------------------------------

_DOF_POSITION = re.compile(
    r"(\d{2})\s(\d{2})\s(\d{2}(?:\.\d+)?)([NS])\s+(\d{3})\s(\d{2})\s(\d{2}(?:\.\d+)?)([EW])"
)
_DOF_TAIL = re.compile(r"^\s+(?P<type>.*?)\s+(?P<qty>\d+)\s+(?P<agl>\d{1,5})\s+(?P<amsl>\d{1,5})\b(?P<rest>.*)$")
_DOF_ACTION = re.compile(r"\s([ACD])\s+\d{7}\b")


def _inside(bbox: tuple[float, float, float, float] | None, west: float, south: float, east: float, north: float) -> bool:
    if bbox is None:
        return True
    return not (east < bbox[0] or west > bbox[2] or north < bbox[1] or south > bbox[3])


def read_dof(path: str | Path, bbox: tuple[float, float, float, float] | None = None) -> LoadResult:
    """FAA Digital Obstacle File (.dat). Heights are feet AGL; dismantled records (action D) are skipped.

    ``bbox`` = (west, south, east, north) in degrees limits what is kept (the national file is large).
    """
    path = Path(path)
    result = LoadResult(source=str(path), height_field="AGL (feet)")
    with path.open("r", encoding="latin-1", errors="replace") as handle:
        for line in handle:
            match = _DOF_POSITION.search(line)
            if match is None:
                continue
            tail = _DOF_TAIL.match(line[match.end():])
            if tail is None:
                result.skipped += 1
                continue
            result.records += 1
            rest = tail.group("rest")
            action = _DOF_ACTION.search(rest)
            if action is not None and action.group(1) == "D":
                result.skipped += 1  # dismantled
                continue
            latitude = int(match.group(1)) + int(match.group(2)) / 60.0 + float(match.group(3)) / 3600.0
            longitude = int(match.group(5)) + int(match.group(6)) / 60.0 + float(match.group(7)) / 3600.0
            if match.group(4) == "S":
                latitude = -latitude
            if match.group(8) == "W":
                longitude = -longitude
            if not _inside(bbox, longitude, latitude, longitude, latitude):
                result.outside += 1
                continue
            agl_ft = float(tail.group("agl"))
            assumed = agl_ft <= 0.0
            height = DEFAULT_OBSTACLE_HEIGHT_M if assumed else agl_ft * FEET_TO_METRES
            identifier = line[:9].strip()
            kind_text = tail.group("type").strip()
            result.obstacles.append(
                Obstacle("point", [[(longitude, latitude)]], height, f"{kind_text} {identifier}".strip(), path.name, assumed)
            )
            result.assumed += int(assumed)
    if result.records == 0:
        raise ObstacleError(
            f"'{path.name}' has no DOF obstacle records (expected lines with a position such as '31 35 47.00N 085 15 44.00W')."
        )
    return result


def _sniff_delimiter(sample: str) -> str:
    counts = {delimiter: sample.count(delimiter) for delimiter in (",", ";", "\t")}
    return max(counts, key=lambda key: counts[key]) if any(counts.values()) else ","


def read_csv_obstacles(
    path: str | Path,
    height_field: str | None = None,
    height_units: str = "Meters",
    default_height_m: float = DEFAULT_OBSTACLE_HEIGHT_M,
    bbox: tuple[float, float, float, float] | None = None,
) -> LoadResult:
    """CSV with latitude and longitude in decimal degrees (WGS84) and a height above ground column."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    if not lines:
        raise ObstacleError(f"'{path.name}' is empty.")
    reader = csv.DictReader(lines, delimiter=_sniff_delimiter(lines[0]))
    names = list(reader.fieldnames or [])
    lat_name = find_field(names, None, LAT_NAMES)
    lon_name = find_field(names, None, LON_NAMES)
    if not lat_name or not lon_name:
        raise ObstacleError(
            f"'{path.name}' needs latitude and longitude columns in decimal degrees (for example lat and lon). "
            f"Columns found: {', '.join(names) or 'none'}."
        )
    if height_field and not find_field(names, height_field, ()):
        raise ObstacleError(f"'{path.name}' has no column '{height_field}'. Columns found: {', '.join(names)}.")
    height_name = find_field(names, height_field, HEIGHT_FIELD_CANDIDATES)
    name_name = find_field(names, None, NAME_FIELD_CANDIDATES)
    result = LoadResult(source=str(path), height_field=height_name)
    if not height_name:
        result.notes.append(
            f"'{path.name}' has no height column: every obstacle gets the default height of {default_height_m:g} m."
        )
    for row in reader:
        result.records += 1
        try:
            latitude = float(str(row[lat_name]).replace(",", "."))
            longitude = float(str(row[lon_name]).replace(",", "."))
        except (TypeError, ValueError):
            result.skipped += 1
            continue
        if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
            result.skipped += 1
            continue
        if not _inside(bbox, longitude, latitude, longitude, latitude):
            result.outside += 1
            continue
        height = parse_height(row.get(height_name), height_units, height_name) if height_name else None
        assumed = height is None
        label = str(row.get(name_name) or "").strip() if name_name else ""
        result.obstacles.append(
            Obstacle("point", [[(longitude, latitude)]], default_height_m if assumed else height, label, path.name, assumed)
        )
        result.assumed += int(assumed)
    if result.records == 0:
        raise ObstacleError(f"'{path.name}' has no data rows.")
    return result


def _geometry_parts(geometry: dict[str, Any]) -> list[tuple[str, list[list[tuple[float, float]]]]]:
    """GeoJSON geometry -> [(kind, parts)], one entry per point, line or polygon."""
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    result: list[tuple[str, list[list[tuple[float, float]]]]] = []

    def pt(item: Sequence[float]) -> tuple[float, float]:
        return (float(item[0]), float(item[1]))

    if kind == "Point":
        result.append(("point", [[pt(coordinates)]]))
    elif kind == "MultiPoint":
        result.extend(("point", [[pt(item)]]) for item in coordinates)
    elif kind == "LineString":
        result.append(("line", [[pt(item) for item in coordinates]]))
    elif kind == "MultiLineString":
        result.append(("line", [[pt(item) for item in line] for line in coordinates]))
    elif kind == "Polygon":
        result.append(("polygon", [[pt(item) for item in ring] for ring in coordinates]))
    elif kind == "MultiPolygon":
        for polygon in coordinates:
            result.append(("polygon", [[pt(item) for item in ring] for ring in polygon]))
    elif kind == "GeometryCollection":
        for inner in geometry.get("geometries", []):
            result.extend(_geometry_parts(inner))
    return result


def read_geojson_obstacles(
    path: str | Path,
    height_field: str | None = None,
    height_units: str = "Meters",
    default_height_m: float = DEFAULT_OBSTACLE_HEIGHT_M,
    bbox: tuple[float, float, float, float] | None = None,
) -> LoadResult:
    """GeoJSON (WGS84) points, lines and polygons. OpenStreetMap tags are understood: ``height`` and ``building:levels``."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ObstacleError(f"'{path.name}' is not valid GeoJSON: {error}") from error
    if isinstance(data, dict) and data.get("type") == "FeatureCollection":
        features = data.get("features", [])
    elif isinstance(data, dict) and data.get("type") == "Feature":
        features = [data]
    elif isinstance(data, dict) and "coordinates" in data:
        features = [{"type": "Feature", "geometry": data, "properties": {}}]
    else:
        raise ObstacleError(f"'{path.name}' is not a GeoJSON FeatureCollection.")
    result = LoadResult(source=str(path))
    property_names: set[str] = set()
    for feature in features[:2000]:
        property_names.update((feature.get("properties") or {}).keys())
    if height_field and not find_field(property_names, height_field, ()):
        raise ObstacleError(
            f"'{path.name}' has no property '{height_field}'. Properties found: {', '.join(sorted(property_names)) or 'none'}."
        )
    height_name = find_field(property_names, height_field, HEIGHT_FIELD_CANDIDATES)
    name_name = find_field(property_names, None, NAME_FIELD_CANDIDATES)
    result.height_field = height_name
    for feature in features:
        geometry = feature.get("geometry") or {}
        properties = feature.get("properties") or {}
        for kind, parts in _geometry_parts(geometry):
            result.records += 1
            xs = [p[0] for part in parts for p in part]
            ys = [p[1] for part in parts for p in part]
            if not xs:
                result.skipped += 1
                continue
            if not _inside(bbox, min(xs), min(ys), max(xs), max(ys)):
                result.outside += 1
                continue
            height = parse_height(properties.get(height_name), height_units, height_name) if height_name else None
            if height is None and not height_field:
                levels = parse_height(properties.get("building:levels"), "Meters")
                if levels is not None:
                    height = levels * 3.0
            assumed = height is None
            label = str(properties.get(name_name) or "").strip() if name_name else ""
            if not label:
                label = str(properties.get("man_made") or properties.get("power") or properties.get("building") or "").strip()
            result.obstacles.append(
                Obstacle(kind, parts, default_height_m if assumed else height, label, path.name, assumed)
            )
            result.assumed += int(assumed)
    if result.records == 0:
        raise ObstacleError(f"'{path.name}' holds no features.")
    return result


def read_obstacle_file(
    path: str | Path,
    height_field: str | None = None,
    height_units: str = "Meters",
    default_height_m: float = DEFAULT_OBSTACLE_HEIGHT_M,
    bbox: tuple[float, float, float, float] | None = None,
) -> LoadResult:
    """Read an obstacle file by its extension (.dat = FAA DOF, .csv, .geojson / .json)."""
    path = Path(path)
    suffix = path.suffix.lower()
    if not path.is_file():
        raise ObstacleError(f"Obstacle file does not exist: {path}")
    if suffix == ".dat":
        return read_dof(path, bbox)
    if suffix in (".csv", ".txt"):
        return read_csv_obstacles(path, height_field, height_units, default_height_m, bbox)
    if suffix in (".geojson", ".json"):
        return read_geojson_obstacles(path, height_field, height_units, default_height_m, bbox)
    raise ObstacleError(
        f"'{path.name}' is not a supported obstacle file. Use an FAA DOF (.dat), a CSV list, or GeoJSON. "
        "Shapefile, GeoPackage and geodatabase layers are chosen as an Obstacles layer instead."
    )


def is_obstacle_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SOURCE_SUFFIXES


def project_obstacles(
    obstacles: Iterable[Obstacle],
    transform: Callable[[list[float], list[float]], tuple[list[float], list[float]]],
) -> list[Obstacle]:
    """Obstacles with latitude/longitude coordinates -> the analysis CRS. ``transform(lons, lats) -> (xs, ys)``."""
    obstacles = list(obstacles)
    lons: list[float] = []
    lats: list[float] = []
    for obstacle in obstacles:
        for part in obstacle.parts:
            for x, y in part:
                lons.append(x)
                lats.append(y)
    if not lons:
        return []
    xs, ys = transform(lons, lats)
    result: list[Obstacle] = []
    index = 0
    for obstacle in obstacles:
        parts: list[list[tuple[float, float]]] = []
        for part in obstacle.parts:
            parts.append([(float(xs[index + k]), float(ys[index + k])) for k in range(len(part))])
            index += len(part)
        result.append(Obstacle(obstacle.kind, parts, obstacle.agl_m, obstacle.name, obstacle.source, obstacle.assumed))
    return result


# ---------------------------------------------------------------------------
# The obstacle set used by the analysis
# ---------------------------------------------------------------------------


def _compass(bearing: float) -> str:
    return ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[int(((bearing % 360.0) + 22.5) // 45.0) % 8]


def _densify(part: Sequence[tuple[float, float]], step: float, closed: bool = False) -> list[tuple[float, float]]:
    """Points along a polyline, no further apart than ``step``."""
    points = list(part)
    if closed and points and points[0] != points[-1]:
        points.append(points[0])
    if len(points) < 2:
        return list(points)
    result: list[tuple[float, float]] = [points[0]]
    for (x0, y0), (x1, y1) in zip(points[:-1], points[1:]):
        length = math.hypot(x1 - x0, y1 - y0)
        pieces = max(1, int(math.ceil(length / step)))
        for k in range(1, pieces + 1):
            fraction = k / pieces
            result.append((x0 + (x1 - x0) * fraction, y0 + (y1 - y0) * fraction))
    return result


@dataclass
class VerticalSamples:
    """Obstacle points in one read window with the elevation of their tops (for the exact approach test)."""

    x: np.ndarray
    y: np.ndarray
    top_z: np.ndarray
    agl: np.ndarray
    owner: np.ndarray
    obstacles: list[Obstacle]
    buffer_m: float

    def __len__(self) -> int:
        return int(self.x.size)

    def describe(self, index: int) -> str:
        obstacle = self.obstacles[int(self.owner[index])]
        label = obstacle.name or {"point": "Obstacle", "line": "Wire or line", "polygon": "Structure"}[obstacle.kind]
        note = " (assumed height)" if obstacle.assumed else ""
        return f"{label}, {obstacle.agl_m:.0f} m AGL{note}"

    def nearest(self, x: float, y: float, max_distance_m: float) -> tuple[float, str] | None:
        """(distance to the obstacle's edge in metres, description with direction) of the closest one, or None."""
        if not len(self):
            return None
        dx = self.x - x
        dy = self.y - y
        distance = np.hypot(dx, dy)
        index = int(np.argmin(distance))
        edge = max(0.0, float(distance[index]) - self.buffer_m)
        if edge > max_distance_m:
            return None
        bearing = math.degrees(math.atan2(dx[index], dy[index])) % 360.0
        return edge, f"{self.describe(index)}, {edge:.0f} m to the {_compass(bearing)}"

    def sector_angles(
        self,
        x: float,
        y: float,
        reference_z: float,
        first_distance_m: float,
        range_m: float,
        centres: np.ndarray,
        half_width_deg: float,
    ) -> list[tuple[float, float, float, float, str, float] | None]:
        """For every sector centre: the steepest listed obstacle as (angle, x, y, distance, text, top elevation), or None."""
        if not len(self):
            return [None] * len(centres)
        dx = self.x - x
        dy = self.y - y
        distance = np.hypot(dx, dy)
        effective = np.maximum(distance - self.buffer_m, first_distance_m)
        keep = np.nonzero(effective <= range_m)[0]
        if keep.size == 0:
            return [None] * len(centres)
        bearing = np.degrees(np.arctan2(dx[keep], dy[keep])) % 360.0
        angle = np.degrees(np.arctan2(self.top_z[keep] - reference_z, effective[keep]))
        result: list[tuple[float, float, float, float, str, float] | None] = []
        for centre in centres:
            difference = np.abs((bearing - centre + 180.0) % 360.0 - 180.0)
            member = np.nonzero(difference <= half_width_deg + 1.0e-9)[0]
            if member.size == 0:
                result.append(None)
                continue
            best = member[int(np.argmax(angle[member]))]
            index = int(keep[best])
            result.append((float(angle[best]), float(self.x[index]), float(self.y[index]), float(distance[index]), self.describe(index), float(self.top_z[index])))
        return result


class ObstacleSet:
    """All listed obstacles in the analysis CRS, with a horizontal safety buffer around each."""

    def __init__(self, obstacles: Sequence[Obstacle], buffer_m: float = DEFAULT_OBSTACLE_BUFFER_M, cell_m: float = 1.0):
        self.obstacles = list(obstacles)
        self.buffer_m = max(0.0, float(buffer_m))
        self.cell_m = float(cell_m)
        bounds = [obstacle.bounds() for obstacle in self.obstacles]
        self._bounds = np.array(bounds, dtype=np.float64).reshape(-1, 4)
        xs: list[float] = []
        ys: list[float] = []
        agl: list[float] = []
        owner: list[int] = []
        for index, obstacle in enumerate(self.obstacles):
            if obstacle.kind == "point":
                points = [obstacle.parts[0][0]]
            elif obstacle.kind == "line":
                points = [point for part in obstacle.parts for point in _densify(part, SAMPLE_STEP_M)]
            else:
                points = [point for ring in obstacle.parts for point in _densify(ring, SAMPLE_STEP_M, closed=True)]
            for x, y in points:
                xs.append(x)
                ys.append(y)
                agl.append(obstacle.agl_m)
                owner.append(index)
        self._sx = np.array(xs, dtype=np.float64)
        self._sy = np.array(ys, dtype=np.float64)
        self._sagl = np.array(agl, dtype=np.float32)
        self._sowner = np.array(owner, dtype=np.int32)

    def __len__(self) -> int:
        return len(self.obstacles)

    def __bool__(self) -> bool:
        return bool(self.obstacles)

    @property
    def assumed_count(self) -> int:
        return sum(1 for obstacle in self.obstacles if obstacle.assumed)

    def counts(self) -> dict[str, int]:
        result = {"point": 0, "line": 0, "polygon": 0}
        for obstacle in self.obstacles:
            result[obstacle.kind] += 1
        return result

    def tallest(self) -> Obstacle | None:
        return max(self.obstacles, key=lambda obstacle: obstacle.agl_m) if self.obstacles else None

    def describe(self) -> str:
        if not self.obstacles:
            return "none"
        counts = self.counts()
        parts = [f"{counts[kind]:,} {label}" for kind, label in (("point", "points"), ("line", "lines"), ("polygon", "polygons")) if counts[kind]]
        tall = self.tallest()
        text = f"{len(self):,} obstacles ({', '.join(parts)}), buffer {self.buffer_m:g} m, tallest {tall.agl_m:.0f} m AGL"
        if self.assumed_count:
            text += f", {self.assumed_count:,} with an assumed height"
        return text

    def digest(self) -> str:
        if not self.obstacles:
            return "none"
        sha = hashlib.sha1()
        for obstacle in self.obstacles:
            sha.update(repr((obstacle.kind, round(obstacle.agl_m, 2), [[(round(x, 1), round(y, 1)) for x, y in part] for part in obstacle.parts])).encode())
        return f"{len(self.obstacles)}:{self.buffer_m:g}:{sha.hexdigest()[:16]}"

    def markers(self, limit: int = MAX_MARKERS) -> list[dict[str, Any]]:
        """Representative points for the report map: tallest first when there are more than ``limit``."""
        chosen = sorted(self.obstacles, key=lambda obstacle: -obstacle.agl_m)[:limit]
        result = []
        for obstacle in chosen:
            x, y = obstacle.representative_point()
            result.append({"x": x, "y": y, "agl": obstacle.agl_m, "name": obstacle.name, "kind": obstacle.kind, "assumed": obstacle.assumed})
        return result

    def _select(self, west: float, south: float, east: float, north: float, margin: float) -> np.ndarray:
        if not len(self._bounds):
            return np.zeros(0, dtype=np.int64)
        b = self._bounds
        return np.nonzero(
            (b[:, 2] >= west - margin) & (b[:, 0] <= east + margin) & (b[:, 3] >= south - margin) & (b[:, 1] <= north + margin)
        )[0]

    def height_raster(self, geometry: GridGeometry) -> np.ndarray:
        """Height above ground (m) of the obstacle footprints on the grid; 0 where there is none."""
        out = np.zeros((geometry.height, geometry.width), dtype=np.float32)
        if not self.obstacles:
            return out
        cell = geometry.cell_size_m
        radius = max(self.buffer_m, 0.75 * cell)
        pad = int(math.ceil(radius / cell)) + 2
        selected = self._select(geometry.x_origin, geometry.y_min, geometry.x_max, geometry.y_origin, radius + cell)
        for index in selected:
            obstacle = self.obstacles[int(index)]
            x0, y0, x1, y1 = obstacle.bounds()
            col0 = max(0, int(math.floor((x0 - geometry.x_origin) / geometry.pixel_width)) - pad)
            col1 = min(geometry.width, int(math.floor((x1 - geometry.x_origin) / geometry.pixel_width)) + pad + 1)
            row0 = max(0, int(math.floor((y1 - geometry.y_origin) / geometry.pixel_height)) - pad)
            row1 = min(geometry.height, int(math.floor((y0 - geometry.y_origin) / geometry.pixel_height)) + pad + 1)
            if col1 <= col0 or row1 <= row0:
                continue
            sub = GridGeometry(
                geometry.x_origin + col0 * geometry.pixel_width,
                geometry.y_origin + row0 * geometry.pixel_height,
                geometry.pixel_width,
                geometry.pixel_height,
                col1 - col0,
                row1 - row0,
            )
            if obstacle.kind == "point":
                x, y = obstacle.parts[0][0]
                cols = sub.x_origin + (np.arange(sub.width) + 0.5) * sub.pixel_width
                rows = sub.y_origin + (np.arange(sub.height) + 0.5) * sub.pixel_height
                mask = (cols[None, :] - x) ** 2 + (rows[:, None] - y) ** 2 <= radius * radius
            elif obstacle.kind == "line":
                mask = rasterize_lines(obstacle.parts, sub, 2.0 * radius)
            else:
                mask = rasterize_polygons([obstacle.parts], sub) | rasterize_lines(obstacle.parts, sub, 2.0 * radius)
            region = out[row0:row1, col0:col1]
            np.maximum(region, np.where(mask, np.float32(obstacle.agl_m), np.float32(0.0)), out=region)
        return out

    def vertical_samples(self, geometry: GridGeometry, dtm: np.ndarray, dtm_valid: np.ndarray) -> VerticalSamples | None:
        """Obstacle points inside the grid with the elevation of their tops (ground from the DTM + AGL)."""
        if not self.obstacles or not self._sx.size:
            return None
        keep = np.nonzero(
            (self._sx >= geometry.x_origin) & (self._sx < geometry.x_max) & (self._sy <= geometry.y_origin) & (self._sy > geometry.y_min)
        )[0]
        if keep.size == 0:
            return None
        cols = np.floor((self._sx[keep] - geometry.x_origin) / geometry.pixel_width).astype(np.int64)
        rows = np.floor((self._sy[keep] - geometry.y_origin) / geometry.pixel_height).astype(np.int64)
        good = (rows >= 0) & (rows < geometry.height) & (cols >= 0) & (cols < geometry.width)
        keep, rows, cols = keep[good], rows[good], cols[good]
        valid = dtm_valid[rows, cols]
        keep, rows, cols = keep[valid], rows[valid], cols[valid]
        if keep.size == 0:
            return None
        ground = dtm[rows, cols].astype(np.float64)
        agl = self._sagl[keep].astype(np.float64)
        return VerticalSamples(
            x=self._sx[keep], y=self._sy[keep], top_z=ground + agl, agl=agl, owner=self._sowner[keep],
            obstacles=self.obstacles, buffer_m=self.buffer_m,
        )


def build_set(obstacles: Sequence[Obstacle], buffer_m: float, cell_m: float) -> ObstacleSet | None:
    """An ObstacleSet, or None when the list is empty."""
    return ObstacleSet(obstacles, buffer_m, cell_m) if obstacles else None


@dataclass
class ObstacleLoad:
    """What the readers hand to the analysis: the set plus a plain-language description and any warnings."""

    obstacles: ObstacleSet | None
    description: str = "none"
    digest: str = "none"
    notes: list[str] = field(default_factory=list)  # shown as warnings
    results: list[LoadResult] = field(default_factory=list)


def finish_load(
    projected: list[Obstacle],
    results: list[LoadResult],
    labels: list[str],
    buffer_m: float,
    cell_m: float,
    extra_notes: list[str] | None = None,
) -> ObstacleLoad:
    """Build the ObstacleLoad from projected obstacles and the per-source results."""
    notes = list(extra_notes or [])
    for result in results:
        notes.extend(result.notes)
        if result.outside and not any(item.source == Path(result.source).name for item in projected):
            notes.append(f"No obstacle in '{Path(result.source).name}' lies within the DTM area.")
        if result.skipped:
            notes.append(f"{result.skipped:,} record(s) of '{Path(result.source).name}' were skipped (no usable position, or dismantled).")
    obstacle_set = build_set(projected, buffer_m, cell_m)
    if obstacle_set is None:
        if labels:
            notes.append("The obstacle sources hold no obstacle inside the DTM area, so none is applied.")
        return ObstacleLoad(None, "none", "none", notes, results)
    fields = sorted({result.height_field for result in results if result.height_field})
    description = f"{', '.join(labels)}: {obstacle_set.describe()}" + (f"; heights from {', '.join(fields)}" if fields else "")
    return ObstacleLoad(obstacle_set, description, obstacle_set.digest(), notes, results)


def load_obstacle_files(
    paths: Sequence[str | Path],
    bbox_ll: tuple[float, float, float, float] | None,
    transform: Callable[[list[float], list[float]], tuple[list[float], list[float]]],
    buffer_m: float,
    cell_m: float,
    height_field: str | None = None,
    height_units: str = "Meters",
    default_height_m: float = DEFAULT_OBSTACLE_HEIGHT_M,
) -> ObstacleLoad:
    """Read FAA DOF / CSV / GeoJSON files, keep what lies in ``bbox_ll`` (degrees) and project it with ``transform``."""
    results: list[LoadResult] = []
    projected: list[Obstacle] = []
    for path in paths:
        result = read_obstacle_file(path, height_field, height_units, default_height_m, bbox_ll)
        results.append(result)
        projected.extend(project_obstacles(result.obstacles, transform))
    return finish_load(projected, results, [Path(p).name for p in paths], buffer_m, cell_m)
