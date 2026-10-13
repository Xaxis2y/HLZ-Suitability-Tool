# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Large-area benchmark: proves memory stays flat and measures throughput.

A procedural reader generates deterministic 1 m terrain (rolling fields,
forest blocks, buildings, ditches) for ANY window on demand, so a city-sized
area can be simulated without data on disk. Only NumPy/SciPy are needed, so
it runs in the hlz-arcpy-v04 clone.

    python tests\\benchmark_large_area.py --km 10 --log logs\\hlz_benchmark_v0.10.3.log

--km 10 = 10 x 10 km = 100 million cells (the DTM size from the v0.6 report).
The log reports cells/second and peak memory, from which the run time for a
real area (e.g. Ottawa, about 2,800 km2) can be estimated.
"""

from __future__ import annotations

import argparse
import platform
import sys
import tempfile
import time
import tracemalloc
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hlzcore.config import load_aircraft_profile, load_doctrine_profile, load_mission_profile, profiles_root  # noqa: E402
from hlzcore.models import GridGeometry  # noqa: E402
from hlzcore.pipeline import RunOptions, run_tiled_pipeline  # noqa: E402

LOG: list[str] = []


def log(message: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} | {message}"
    LOG.append(line)
    print(line, flush=True)


def _hash_noise(rows: np.ndarray, cols: np.ndarray, seed: int) -> np.ndarray:
    value = (rows * 73856093) ^ (cols * 19349663) ^ (seed * 83492791)
    value = (value ^ (value >> 13)) * 1274126177
    return ((value >> 8) & 0xFFFF).astype(np.float32) / 65535.0


class ProceduralTileReader:
    """Deterministic synthetic 1 m DTM/DSM of arbitrary size, generated per window."""

    def __init__(self, size_cells: int, cell: float = 1.0):
        self.geometry = GridGeometry(500_000.0, 5_050_000.0, cell, -cell, size_cells, size_cells)
        self.window = (0, size_cells, 0, size_cells)
        self.has_dsm = True
        self.crs_description = "synthetic (UTM-like metres)"
        self.dtm_source = f"procedural:{size_cells}"
        self.dsm_source = "procedural-dsm"
        self.aoi_digest = "none"
        self.notes: list[str] = []
        self.cells_read = 0

    def read(self, row0: int, row1: int, col0: int, col1: int):  # type: ignore[no-untyped-def]
        rows, cols = np.mgrid[row0:row1, col0:col1]
        r = rows.astype(np.float32)
        c = cols.astype(np.float32)
        dtm = 80.0 + 6.0 * np.sin(r / 410.0) + 4.0 * np.cos(c / 330.0) + 0.6 * np.sin((r + c) / 230.0)
        ditch = (np.abs((r % 900.0) - 450.0) < 1.5) & ((c % 1300.0) < 700.0)
        dtm = np.where(ditch, dtm - 0.8, dtm).astype(np.float32)
        block_r = (rows // 400).astype(np.int64)
        block_c = (cols // 400).astype(np.int64)
        block_kind = _hash_noise(block_r, block_c, 7)
        dsm = dtm.copy()
        forest = block_kind < 0.35
        dsm[forest] += 12.0 + 6.0 * _hash_noise(rows, cols, 3)[forest]
        urban = (block_kind > 0.8) & ((rows % 60) < 35) & ((cols % 60) < 35)
        dsm[urban] += 15.0
        shrubs = (_hash_noise(rows, cols, 11) > 0.99997) & ~forest
        dsm[shrubs] += 2.5
        valid = np.ones(dtm.shape, dtype=bool)
        self.cells_read += dtm.size
        return dtm, valid, dsm.astype(np.float32), valid.copy()

    def aoi_mask(self, row0: int, row1: int, col0: int, col1: int):  # type: ignore[no-untyped-def]
        return None


class CountingWriter:
    def begin_rasters(self, grid, specs, resume):  # type: ignore[no-untyped-def]
        self.tiles = 0

    def write_raster_tile(self, name, array, tile_geometry, tile):  # type: ignore[no-untyped-def]
        self.tiles += 1

    def finish_rasters(self):  # type: ignore[no-untyped-def]
        return {}

    def write_vectors(self, candidates, aircraft, top_sectors):  # type: ignore[no-untyped-def]
        return {}

    def to_latlon(self, xs, ys):  # type: ignore[no-untyped-def]
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--km", type=float, default=10.0, help="square area edge in km (1 m cells)")
    parser.add_argument("--tile-cells", type=int, default=None, help="override max_input_cells (cells per tile)")
    parser.add_argument("--log", default=str(ROOT / "logs" / "hlz_benchmark_v0.10.3.log"))
    args = parser.parse_args()
    size = int(args.km * 1000)
    aircraft = load_aircraft_profile(profiles_root() / "aircraft" / "generic_medium.json")
    mission = load_mission_profile(profiles_root() / "missions" / "screening.json")
    if args.tile_cells:
        mission = replace(mission, max_input_cells=args.tile_cells)
    mission = replace(mission, max_candidates=100_000)
    doctrine = load_doctrine_profile(profiles_root() / "doctrine" / "generic_planning.json")
    reader = ProceduralTileReader(size)
    log(f"python {sys.version.split()[0]} | {platform.platform()} | env {Path(sys.prefix).name}")
    log(f"area {args.km:g} x {args.km:g} km = {size * size:,} cells; tile budget {mission.max_input_cells:,} cells")
    full_float32_gb = size * size * 4 / 1e9
    log(f"(holding this DTM + DSM in memory as float32 would need {2 * full_float32_gb:.1f} GB)")

    tracemalloc.start()
    started = time.perf_counter()
    result = run_tiled_pipeline(
        reader,
        aircraft,
        mission,
        doctrine,
        RunOptions(run_name="benchmark", diagnostic_rasters="NEVER"),
        Path(tempfile.mkdtemp(prefix="hlz_bench_")),
        CountingWriter(),
        progress=lambda message: log("  " + message) if message.startswith("Tile ") else None,
    )
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    cells = size * size
    rate = cells / elapsed / 1e6
    log(f"done in {elapsed / 60:.1f} min -> {rate:.2f} million cells/s (incl. buffer reads)")
    log(f"peak traced memory {peak / 1e9:.2f} GB (independent of total area)")
    log(f"tiles {result.metadata['tiling']['tile_count']} | candidates {len(result.candidates):,} | "
        f"ratings {result.metadata['results']['rating_counts']}")
    log(f"read amplification {reader.cells_read / cells:.2f}x (approach buffer around each tile)")
    for name, km2 in (("Ottawa (city, ~2,790 km2)", 2790), ("Greater Toronto Area (~7,125 km2)", 7125)):
        hours = km2 * 1e6 / (rate * 1e6) / 3600
        log(f"estimate at this speed: {name}: about {hours:.1f} h at 1 m")
    header = "# SPDX-License-Identifier: GPL-2.0-or-later\n# Copyright (c) 2026 Eui Soo SON\n"
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(header + "\n".join(LOG) + "\n", encoding="utf-8")
    print(f"Log written to {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
