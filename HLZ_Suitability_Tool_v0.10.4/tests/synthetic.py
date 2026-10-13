# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Deterministic synthetic DTM/DSM scene used by every test level.

Scene (1 m cells, UTM zone 18N near Ottawa, north-up, 2.4 x 2.4 km;
the features sit in the central 600 x 600 m block so that 1 km approach
rays stay inside the data):

* open, nearly flat field in the west          -> expected good HLZs
* a 9-10 degree slope band in the south-east     -> upslope-only candidates
* a steep hill in the north-east                 -> fails slope
* a forest block (DSM +18 m) in the middle       -> obstacles / blocked approaches
* a 12 m building north of the field             -> blocks northern approaches
* a 0.8 m deep ditch crossing the field          -> roughness rejection
* a small NoData hole                            -> coverage handling
"""

from __future__ import annotations

import numpy as np

EPSG = 32618
X_ORIGIN = 445_000.0
Y_ORIGIN = 5_032_400.0
CELL = 1.0
SIZE = 2400
CORE = 900  # offset of the 600 m feature block; the margin supplies approach data


def build_scene(size: int = SIZE, cell: float = CELL) -> dict[str, np.ndarray | float | int]:
    grid_rows, grid_cols = np.mgrid[0:size, 0:size].astype(np.float64)
    x = grid_cols * cell
    y = (size - grid_rows) * cell
    offset = CORE if size >= CORE * 2 + 600 else 0
    rows = grid_rows - offset
    cols = grid_cols - offset
    dtm = 100.0 + 0.004 * x + 0.002 * y  # ~0.25 deg regional tilt
    # Slope band in the south-east (~9.5 deg rising to the east).
    band = (cols > 380) & (cols <= 600) & (rows > 380) & (rows <= 600)
    dtm = dtm + np.where(band, np.tan(np.radians(9.5)) * (cols - 380) * cell, 0.0)
    beyond = (cols > 600) & (rows > 380) & (rows <= 600)
    dtm = dtm + np.where(beyond, np.tan(np.radians(9.5)) * 220.0 * cell, 0.0)
    # Steep hill in the north-east.
    hill = 40.0 * np.exp(-(((cols - 500) ** 2 + (rows - 120) ** 2) / (2 * 45.0**2)))
    dtm = dtm + hill
    # Ditch across the field (rows 300-302, cols 40-260).
    ditch = (rows >= 300) & (rows <= 302) & (cols >= 40) & (cols <= 260)
    dtm = np.where(ditch, dtm - 0.8, dtm)

    dsm = dtm.copy()
    forest = (cols >= 270) & (cols <= 340) & (rows >= 150) & (rows <= 450)
    dsm = np.where(forest, dsm + 18.0, dsm)
    building = (cols >= 120) & (cols <= 160) & (rows >= 60) & (rows <= 80)
    dsm = np.where(building, dsm + 12.0, dsm)
    # Scattered 2 m shrubs in the north-west corner.
    rng = np.random.default_rng(42)
    shrubs = (rng.random((size, size)) < 0.01) & (cols >= 0) & (cols < 100) & (rows >= 0) & (rows < 100)
    dsm = np.where(shrubs, dsm + 2.0, dsm)

    dtm = dtm.astype(np.float32)
    dsm = dsm.astype(np.float32)
    hole = (rows >= 500) & (rows <= 530) & (cols >= 60) & (cols <= 90)
    dtm[hole] = np.nan
    dsm[hole] = np.nan
    return {
        "dtm": dtm,
        "dsm": dsm,
        "epsg": EPSG,
        "x_origin": X_ORIGIN,
        "y_origin": Y_ORIGIN,
        "cell": cell,
    }


def write_geotiffs(folder, nodata: float = -9999.0):  # type: ignore[no-untyped-def]
    """Write dtm.tif / dsm.tif with rasterio (CLI tests only)."""
    from pathlib import Path

    import rasterio
    from rasterio.transform import from_origin

    scene = build_scene()
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    transform = from_origin(scene["x_origin"], scene["y_origin"], scene["cell"], scene["cell"])
    paths = {}
    for name in ("dtm", "dsm"):
        array = np.where(np.isfinite(scene[name]), scene[name], nodata).astype(np.float32)
        path = folder / f"{name}.tif"
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=array.shape[1],
            height=array.shape[0],
            count=1,
            dtype="float32",
            crs=f"EPSG:{scene['epsg']}",
            transform=transform,
            nodata=nodata,
        ) as dataset:
            dataset.write(array, 1)
        paths[name] = path
    return paths
