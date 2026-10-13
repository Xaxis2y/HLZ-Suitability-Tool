# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Minimal fake ``arcpy`` for developer smoke tests WITHOUT ArcGIS Pro.

It implements only what the HLZ toolbox calls, backed by rasterio/pyproj.
It proves the toolbox wiring (parameters, validation, IO, cursors) works;
it does NOT prove ArcGIS Pro behaviour. Always confirm with
``tests/run_arcpy_smoke.py`` inside a cloned arcgispro-py3 environment.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import numpy as np

MESSAGES: list[tuple[str, str]] = []
FEATURE_CLASSES: dict[str, dict] = {}
MOSAICS: dict[str, list] = {}


class ExecuteError(Exception):
    pass


def AddMessage(text):  # noqa: N802
    MESSAGES.append(("INFO", str(text)))


def AddWarning(text):  # noqa: N802
    MESSAGES.append(("WARNING", str(text)))


def AddError(text):  # noqa: N802
    MESSAGES.append(("ERROR", str(text)))


def SetProgressor(*_args, **_kwargs):  # noqa: N802
    pass


def SetProgressorLabel(*_args, **_kwargs):  # noqa: N802
    pass


def SetProgressorPosition(*_args, **_kwargs):  # noqa: N802
    pass


def ResetProgressor(*_args, **_kwargs):  # noqa: N802
    pass


def SetParameterAsText(index, text):  # noqa: N802
    MESSAGES.append(("PARAM", f"{index}={text}"))


env = SimpleNamespace(scratchGDB=os.environ.get("TMPDIR", "/tmp"), overwriteOutput=True, isCancelled=False)


class _Filter:
    def __init__(self):
        self.type = None
        self.list = []


class Parameter:
    def __init__(self, displayName, name, datatype, parameterType, direction, category=None, multiValue=False):  # noqa: N803
        self.multiValue = multiValue
        self.values = None
        self.displayName = displayName
        self.name = name
        self.datatype = datatype
        self.parameterType = parameterType
        self.direction = direction
        self.category = category
        self.filter = _Filter()
        self.value = None
        self.enabled = True
        self.altered = False
        self.hasBeenValidated = False
        self.message = None
        self.level = None

    @property
    def valueAsText(self):  # noqa: N802
        if self.values:
            return ";".join(str(v) for v in self.values)
        if self.value is None:
            return None
        if isinstance(self.value, bool):
            return "true" if self.value else "false"
        return str(self.value)

    def hasError(self):  # noqa: N802
        return self.level == "ERROR"

    def hasWarning(self):  # noqa: N802
        return self.level == "WARNING"

    def setErrorMessage(self, text):  # noqa: N802
        self.level, self.message = "ERROR", text

    def setWarningMessage(self, text):  # noqa: N802
        if self.level != "ERROR":
            self.level, self.message = "WARNING", text


class SpatialReference:
    def __init__(self, code=None, wkt=None):
        from pyproj import CRS

        self.crs = CRS.from_epsg(code) if code is not None else CRS.from_wkt(wkt)
        self.factoryCode = self.crs.to_epsg() or 0
        self.name = self.crs.name
        self.type = "Projected" if self.crs.is_projected else "Geographic"
        unit = self.crs.axis_info[0].unit_name if self.crs.axis_info else ""
        self.linearUnitName = "Meter" if unit in ("metre", "meter") else unit
        self.metersPerUnit = 1.0 if self.linearUnitName == "Meter" else 0.0


class Point:
    def __init__(self, X=0.0, Y=0.0):  # noqa: N803
        self.X = float(X)
        self.Y = float(Y)


class Array(list):
    pass


class Extent:
    def __init__(self, XMin, YMin, XMax, YMax, spatial_reference=None):  # noqa: N803
        self.XMin, self.YMin, self.XMax, self.YMax = XMin, YMin, XMax, YMax
        self.spatialReference = spatial_reference
        self.width = XMax - XMin
        self.height = YMax - YMin

    @property
    def polygon(self):
        corners = [Point(self.XMin, self.YMin), Point(self.XMin, self.YMax), Point(self.XMax, self.YMax),
                   Point(self.XMax, self.YMin), Point(self.XMin, self.YMin)]
        return _Geometry("polygon", corners, self.spatialReference)


class _Geometry:
    """Axis-aligned rectangles only (enough for the toolbox's AOI tile tests)."""

    def __init__(self, kind, points, sr):
        self.kind = kind
        self.points = [(p.X, p.Y) for p in points]
        self.spatialReference = sr

    @property
    def extent(self):
        xs = [x for x, _ in self.points]
        ys = [y for _, y in self.points]
        return Extent(min(xs), min(ys), max(xs), max(ys), self.spatialReference)

    @property
    def area(self):
        e = self.extent
        return e.width * e.height

    @property
    def pointCount(self):  # noqa: N802
        return len(self.points)

    def disjoint(self, other):
        a, b = self.extent, other.extent
        return a.XMax <= b.XMin or b.XMax <= a.XMin or a.YMax <= b.YMin or b.YMax <= a.YMin

    def contains(self, other):
        a, b = self.extent, other.extent
        return a.XMin <= b.XMin and a.YMin <= b.YMin and a.XMax >= b.XMax and a.YMax >= b.YMax

    def clip(self, box):
        a = self.extent
        e = Extent(max(a.XMin, box.XMin), max(a.YMin, box.YMin), min(a.XMax, box.XMax), min(a.YMax, box.YMax))
        return e.polygon

    def __iter__(self):
        return iter([[Point(x, y) for x, y in self.points]])

    def projectAs(self, _sr):  # noqa: N802
        return self


def Polygon(array, sr=None):  # noqa: N802
    return _Geometry("polygon", array, sr)


def Polyline(array, sr=None):  # noqa: N802
    return _Geometry("polyline", array, sr)


class PointGeometry:
    def __init__(self, point, sr):
        self.firstPoint = point
        self.points = [(point.X, point.Y)]
        self.spatialReference = sr

    def projectAs(self, target, transformation=""):  # noqa: N802
        from pyproj import Transformer

        transformer = Transformer.from_crs(self.spatialReference.crs, target.crs, always_xy=True)
        x, y = transformer.transform(self.firstPoint.X, self.firstPoint.Y)
        return PointGeometry(Point(x, y), target)


def ListTransformations(*_args, **_kwargs):  # noqa: N802
    return []


class Raster:
    def __init__(self, path):
        import rasterio

        self.catalogPath = path
        with rasterio.open(path) as dataset:
            self.bandCount = dataset.count
            self.width = dataset.width
            self.height = dataset.height
            self.meanCellWidth = abs(dataset.transform.a)
            self.meanCellHeight = abs(dataset.transform.e)
            bounds = dataset.bounds
            self.extent = Extent(bounds.left, bounds.bottom, bounds.right, bounds.top)
            self.spatialReference = SpatialReference(wkt=dataset.crs.to_wkt())
            self.pixelType = "F32" if dataset.dtypes[0].startswith("float") else "S32"
            # Emulate ArcGIS: noDataValue is reported with lossy rounding, so it
            # may not equal the stored sentinel exactly (an earlier smoke-test bug).
            self.noDataValue = None if dataset.nodata is None else float(f"{dataset.nodata:.12g}")


def Describe(path):  # noqa: N802
    if str(path).lower().endswith(".tif"):
        return SimpleNamespace(catalogPath=str(path))
    if str(path) in FEATURE_CLASSES:
        rows = FEATURE_CLASSES[str(path)]["rows"]
        shape_type = FEATURE_CLASSES[str(path)]["type"].capitalize()
        xs = [x for row in rows for x, _ in row[0].points]
        ys = [y for row in rows for _, y in row[0].points]
        extent = Extent(min(xs), min(ys), max(xs), max(ys)) if xs else None
        return SimpleNamespace(catalogPath=str(path), extent=extent, shapeType=shape_type, spatialReference=SpatialReference(32618))
    return SimpleNamespace(catalogPath=str(path))


def RasterToNumPyArray(raster, lower_left, ncols, nrows, nodata_to_value=None):  # noqa: N802
    import rasterio
    from rasterio.windows import Window

    with rasterio.open(raster.catalogPath) as dataset:
        col = int(round((lower_left.X - dataset.bounds.left) / dataset.transform.a))
        row = int(round((dataset.bounds.top - (lower_left.Y + nrows * abs(dataset.transform.e))) / abs(dataset.transform.e)))
        fill = dataset.nodata if dataset.nodata is not None else 0
        array = dataset.read(1, window=Window(col, row, ncols, nrows), boundless=True, fill_value=fill)
        if nodata_to_value is not None and dataset.nodata is not None:
            array = array.astype(np.float64)
            array[array == np.float64(np.float32(dataset.nodata))] = nodata_to_value
        return array


class _OutRaster:
    def __init__(self, array, lower_left, cell_w, cell_h, nodata):
        self.array, self.lower_left, self.cell_w, self.cell_h, self.nodata = array, lower_left, cell_w, cell_h, nodata

    def save(self, path):
        open(path, "wb").close()
        np.save(path + ".npy", self.array)
        with open(path + ".json", "w", encoding="utf-8") as stream:
            json.dump({"ll": [self.lower_left.X, self.lower_left.Y], "cell": self.cell_w, "nodata": float(self.nodata)}, stream)


def NumPyArrayToRaster(array, lower_left, cell_w, cell_h, nodata):  # noqa: N802
    return _OutRaster(array, lower_left, cell_w, cell_h, nodata)


def Exists(path):  # noqa: N802
    return str(path) in FEATURE_CLASSES or os.path.exists(str(path))


class EnvManager:
    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Management:
    @staticmethod
    def CreateFileGDB(folder, name):  # noqa: N802
        os.makedirs(os.path.join(folder, name), exist_ok=True)

    @staticmethod
    def DefineProjection(*_args):  # noqa: N802
        pass

    @staticmethod
    def GetCount(path):  # noqa: N802
        return [len(FEATURE_CLASSES[str(path)]["rows"])]

    @staticmethod
    def BuildRasterAttributeTable(*_args):  # noqa: N802
        pass

    @staticmethod
    def Delete(path):  # noqa: N802
        FEATURE_CLASSES.pop(str(path), None)

    @staticmethod
    def CreateFeatureclass(gdb, name, geometry_type, spatial_reference=None):  # noqa: N802
        FEATURE_CLASSES[os.path.join(gdb, name)] = {"type": geometry_type, "fields": [], "rows": []}

    @staticmethod
    def AddFields(path, fields):  # noqa: N802
        for field in fields:
            if field[1] == "TEXT" and not field[3]:
                raise ValueError(f"TEXT field {field[0]} needs a length")
            if len(field[0]) > 64:
                raise ValueError("field name too long")
        FEATURE_CLASSES[str(path)]["fields"].extend(field[0] for field in fields)

    @staticmethod
    def MosaicToNewRaster(inputs, gdb, name, *_args, **_kwargs):  # noqa: N802
        arrays = [np.load(path + ".npy") for path in inputs]
        meta = [json.load(open(path + ".json", encoding="utf-8")) for path in inputs]
        MOSAICS[os.path.join(gdb, name)] = list(zip(arrays, meta))
        open(os.path.join(gdb, name), "wb").close()

    @staticmethod
    def SetRasterProperties(*_args, **_kwargs):  # noqa: N802
        pass

    @staticmethod
    def CalculateStatistics(*_args, **_kwargs):  # noqa: N802
        pass

    @staticmethod
    def BuildPyramids(*_args, **_kwargs):  # noqa: N802
        pass

    @staticmethod
    def Resample(*_args, **_kwargs):  # noqa: N802
        raise NotImplementedError("fake arcpy: use an aligned DSM")

    ProjectRaster = Resample


management = _Management()


class _InsertCursor:
    def __init__(self, path, fields):
        self.path = str(path)
        self.fields = fields
        known = FEATURE_CLASSES[self.path]["fields"]
        for field in fields:
            if field != "SHAPE@" and field not in known:
                raise RuntimeError(f"Field {field} does not exist in {self.path}")

    def insertRow(self, row):  # noqa: N802
        if len(row) != len(self.fields):
            raise RuntimeError("Row length does not match the field list")
        FEATURE_CLASSES[self.path]["rows"].append(row)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _SearchCursor:
    def __init__(self, path, fields, **kwargs):
        rows = FEATURE_CLASSES[str(path)]["rows"]
        if fields and fields[0] == "SHAPE@XY":
            def centroid(shape):
                xs = [x for x, _ in shape.points]
                ys = [y for _, y in shape.points]
                return (sum(xs) / len(xs), sum(ys) / len(ys))
            self.rows = [[centroid(row[0])] for row in rows]
        else:
            self.rows = [[row[0]] for row in rows]

    def __iter__(self):
        return iter(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


da = SimpleNamespace(InsertCursor=_InsertCursor, SearchCursor=_SearchCursor)


class _NoProject:
    def ArcGISProject(self, *_args):  # noqa: N802
        raise OSError("fake arcpy: no ArcGIS Pro project")


mp = _NoProject()
