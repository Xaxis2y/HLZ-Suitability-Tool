# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Unit tests for v0.7.1: stage timing in the run log/metadata and the calibrated run-time estimate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from hlzcore.pipeline import BACKEND_OVERHEAD_S, estimate_runtime_text  # noqa: E402
from test_core import _run, _scene  # noqa: E402


def _seconds(text: str) -> tuple[float, float]:
    """Parse '3 tile(s), roughly 62 s-3 min' into seconds."""
    span = text.split("roughly ", 1)[1].rstrip(".")
    parts = []
    for piece in span.split("-"):
        value, unit = piece.split()
        parts.append(float(value) * {"s": 1.0, "min": 60.0, "h": 3600.0}[unit])
    return parts[0], parts[1]


def test_generic_estimate_is_unchanged() -> None:
    assert estimate_runtime_text(1_000_000, 1) == "1 tile(s), roughly 2 s-5 s"
    assert estimate_runtime_text(1_000_000, 1, "generic") == estimate_runtime_text(1_000_000, 1)
    assert estimate_runtime_text(1_000_000, 1, "no-such-backend") == estimate_runtime_text(1_000_000, 1)


def test_arcpy_estimate_includes_the_measured_overhead() -> None:
    # The v0.7.0 smoke test: 0.36 Mcells in 1 tile took 84-146 s, 36 tiles over 5.76 Mcells took 222 s.
    one = _seconds(estimate_runtime_text(360_000, 1, "arcpy"))
    assert one[0] <= 84 and one[1] >= 146
    many = _seconds(estimate_runtime_text(5_760_000, 36, "arcpy"))
    assert many[0] <= 222 <= many[1]
    # More tiles never make the estimate smaller.
    assert _seconds(estimate_runtime_text(1_000_000, 9, "arcpy"))[0] > _seconds(estimate_runtime_text(1_000_000, 1, "arcpy"))[0]
    assert set(BACKEND_OVERHEAD_S) >= {"generic", "arcpy"}


def test_stage_timing_is_logged_and_stored() -> None:
    messages: list[str] = []
    result, _ = _run(_scene(), 250_000, progress=messages.append)
    stages = result.metadata["stage_seconds"]
    for name in ("setup", "begin_rasters", "tile_analysis", "tile_read", "raster_tile_write", "checkpoint_save",
                 "post_processing", "finish_rasters", "write_vectors", "reports"):
        assert name in stages and stages[name] >= 0.0, name
    # Reading is part of the analysis, never more than it.
    assert stages["tile_read"] <= stages["tile_analysis"] + 1.0e-6
    assert any(message.startswith("Stage timing (s):") for message in messages)
    # The stages cannot add up to more than the wall time, and the stored file agrees with the returned metadata.
    assert sum(v for k, v in stages.items() if k != "tile_read") <= result.metadata["elapsed_seconds"] + 0.05
    stored = json.loads(Path(result.outputs["metadata"]).read_text(encoding="utf-8"))
    assert stored["stage_seconds"] == stages and stored["elapsed_seconds"] == result.metadata["elapsed_seconds"]


def test_arcpy_reader_selects_the_arcpy_overhead() -> None:
    text = (ROOT / "hlzcore" / "io_arcpy.py").read_text(encoding="utf-8")
    assert 'RUNTIME_BACKEND = "arcpy"' in text
    assert "estimate_runtime_text(cell_count, len(tiles), 'arcpy')" in (ROOT / "toolbox" / "HLZ_Suitability.pyt").read_text(encoding="utf-8")
