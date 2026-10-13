# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Out-of-core tiling support (v0.6.7).

Large areas (a whole city at 1 m is several billion cells) are processed tile
by tile. Only one tile and its approach buffer are in memory at any time, so
the total area is limited by disk space and time, not by RAM.

This module holds the pieces that do not depend on any GIS library:

* ``plan_tiles``         - split the analysis window into non-overlapping tiles
* ``TileCheckpoint``      - per-tile JSON results so an interrupted run resumes
* ``candidate_to_dict`` / ``candidate_from_dict`` - checkpoint serialisation
* ``suppress_across_tiles`` - remove near-duplicate candidates at tile seams
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Iterable

from .models import RATING_ORDER, ApproachSector, Candidate

STATE_FOLDER = "_tiles"
STATE_FILE = "run_state.json"


@dataclass(frozen=True)
class Tile:
    """One tile, in full-raster row/column coordinates (end exclusive)."""

    index: int
    row0: int
    row1: int
    col0: int
    col1: int

    @property
    def cells(self) -> int:
        return (self.row1 - self.row0) * (self.col1 - self.col0)

    @property
    def name(self) -> str:
        return f"tile_{self.index:05d}"


def tile_size_px(max_core_cells: int, halo_px: int, minimum: int = 256) -> int:
    """Tile edge length so that tile + 2 x halo stays within ``max_core_cells``."""
    return max(minimum, int(math.isqrt(int(max_core_cells))) - 2 * halo_px)


def plan_tiles(window: tuple[int, int, int, int], tile_px: int) -> list[Tile]:
    """Split ``(row0, row1, col0, col1)`` into near-equal, non-overlapping tiles."""
    row0, row1, col0, col1 = window
    rows = row1 - row0
    cols = col1 - col0
    if rows <= 0 or cols <= 0:
        return []
    n_rows = max(1, math.ceil(rows / tile_px))
    n_cols = max(1, math.ceil(cols / tile_px))
    row_edges = [row0 + round(rows * index / n_rows) for index in range(n_rows + 1)]
    col_edges = [col0 + round(cols * index / n_cols) for index in range(n_cols + 1)]
    tiles: list[Tile] = []
    for r in range(n_rows):
        for c in range(n_cols):
            tiles.append(
                Tile(
                    index=len(tiles) + 1,
                    row0=row_edges[r],
                    row1=row_edges[r + 1],
                    col0=col_edges[c],
                    col1=col_edges[c + 1],
                )
            )
    return tiles


def run_signature(items: dict[str, Any]) -> str:
    """Stable hash of everything that defines a run (used to validate resume)."""
    text = json.dumps(items, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


# ---------------------------------------------------------------------------
# Candidate (de)serialisation
# ---------------------------------------------------------------------------

_CANDIDATE_FIELDS = {item.name for item in fields(Candidate)}
_SECTOR_FIELDS = {item.name for item in fields(ApproachSector)}


def candidate_to_dict(candidate: Candidate) -> dict[str, Any]:
    data = asdict(candidate)
    data["sectors"] = [
        {key: (None if isinstance(value, float) and not math.isfinite(value) else value)
         for key, value in asdict(sector).items()}
        for sector in candidate.sectors
    ]
    return data


def candidate_from_dict(data: dict[str, Any]) -> Candidate:
    sectors = [
        ApproachSector(
            **{
                key: (math.nan if key == "worst_angle_deg" and value is None else value)
                for key, value in sector.items()
                if key in _SECTOR_FIELDS
            }
        )
        for sector in data.get("sectors", [])
    ]
    values = {key: value for key, value in data.items() if key in _CANDIDATE_FIELDS and key != "sectors"}
    candidate = Candidate(**values)
    candidate.sectors = sectors
    return candidate


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------


class ResumeMismatch(RuntimeError):
    """The run folder holds a checkpoint from a different set of inputs."""


class TileCheckpoint:
    """Per-tile results on disk: ``<run>/_tiles/tile_00001.json``."""

    def __init__(self, output_folder: str | Path, signature: str, tile_count: int):
        self.folder = Path(output_folder) / STATE_FOLDER
        self.signature = signature
        self.tile_count = tile_count
        self.resumed = False

    @property
    def state_path(self) -> Path:
        return self.folder / STATE_FILE

    def open(self) -> list[int]:
        """Create or validate the state; return the indices already completed."""
        self.folder.mkdir(parents=True, exist_ok=True)
        if self.state_path.is_file():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if state.get("signature") != self.signature:
                raise ResumeMismatch(
                    "This run folder contains a checkpoint from different inputs or settings; "
                    "use a new run name"
                )
            done = sorted(
                int(path.stem.split("_")[1])
                for path in self.folder.glob("tile_*.json")
            )
            self.resumed = bool(done)
            return done
        self._write_state(complete=False)
        return []

    def _write_state(self, complete: bool) -> None:
        state = {
            "signature": self.signature,
            "tile_count": self.tile_count,
            "complete": complete,
        }
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def save_tile(self, tile: Tile, candidates: list[Candidate], stats: dict[str, Any]) -> None:
        document = {
            "tile": asdict(tile),
            "stats": stats,
            "candidates": [candidate_to_dict(candidate) for candidate in candidates],
        }
        temporary = self.folder / f"{tile.name}.json.tmp"
        temporary.write_text(json.dumps(document), encoding="utf-8")
        temporary.replace(self.folder / f"{tile.name}.json")

    def load_tile(self, tile: Tile) -> tuple[list[Candidate], dict[str, Any]]:
        document = json.loads((self.folder / f"{tile.name}.json").read_text(encoding="utf-8"))
        return [candidate_from_dict(item) for item in document["candidates"]], document["stats"]

    def mark_complete(self) -> None:
        self._write_state(complete=True)


def checkpoint_status(output_folder: str | Path) -> str:
    """Return ``none``, ``incomplete`` or ``complete`` for a run folder."""
    path = Path(output_folder) / STATE_FOLDER / STATE_FILE
    if not path.is_file():
        return "none"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "none"
    return "complete" if state.get("complete") else "incomplete"


# ---------------------------------------------------------------------------
# Global suppression across tile seams
# ---------------------------------------------------------------------------


def rank_key(candidate: Candidate) -> tuple:
    return (
        RATING_ORDER.index(candidate.rating),
        -candidate.score,
        -candidate.assessed_sector_count,
        -candidate.clear_sector_count,
        -candidate.selection_quality,
        candidate.search_distance_m if candidate.search_distance_m is not None else 0.0,
        candidate.row,
        candidate.col,
    )


def suppress_across_tiles(candidates: Iterable[Candidate], separation_m: float) -> list[Candidate]:
    """Greedy best-first suppression with a spatial hash.

    Each tile already enforces the separation internally; this removes the
    near-duplicates that can appear on both sides of a tile seam.
    """
    ordered = sorted(candidates, key=rank_key)
    if separation_m <= 0:
        return ordered
    bins: dict[tuple[int, int], list[Candidate]] = {}
    kept: list[Candidate] = []
    limit_sq = separation_m * separation_m
    for candidate in ordered:
        bx = int(math.floor(candidate.x / separation_m))
        by = int(math.floor(candidate.y / separation_m))
        clash = False
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in bins.get((bx + dx, by + dy), ()):
                    if (candidate.x - other.x) ** 2 + (candidate.y - other.y) ** 2 < limit_sq - 1.0e-9:
                        clash = True
                        break
                if clash:
                    break
            if clash:
                break
        if clash:
            continue
        kept.append(candidate)
        bins.setdefault((bx, by), []).append(candidate)
    return kept
