# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Unit tests for v0.10.0: one PRIMARY (safest) candidate per site, the others are ALTERNATE."""

from __future__ import annotations

import csv
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from hlzcore.pipeline import ArrayTileReader, RunOptions, run_tiled_pipeline  # noqa: E402
from hlzcore.report import CSV_COLUMNS, write_candidate_csv, write_html_report  # noqa: E402
from test_core import APPROVED, MISSION, TILE_AIRCRAFT, _NullWriter, _scene  # noqa: E402


def _result():  # type: ignore[no-untyped-def]
    mission = replace(MISSION, max_input_cells=10_000_000, max_candidates=5000, max_candidates_per_tile=500)
    return run_tiled_pipeline(ArrayTileReader(_scene()), TILE_AIRCRAFT, mission, APPROVED, RunOptions(), Path(tempfile.mkdtemp()), _NullWriter())


def test_each_site_has_exactly_one_primary_and_it_is_the_best_ranked() -> None:
    result = _result()
    assert result.candidates
    sites: dict[int, list] = {}
    for candidate in result.candidates:
        sites.setdefault(candidate.site_no, []).append(candidate)
    for members in sites.values():
        primaries = [c for c in members if c.site_role == "PRIMARY"]
        assert len(primaries) == 1
        assert primaries[0].rank == min(c.rank for c in members)  # candidates are in rank order: the safest one
        assert all(c.site_primary_id == primaries[0].candidate_id for c in members)
        assert primaries[0].site_alternates == len(members) - 1
        assert all(c.site_role == "ALTERNATE" and c.site_alternates == 0 for c in members if c is not primaries[0])
    assert result.metadata["sites"]["primary_sites"] == len(sites)


def test_csv_carries_the_site_role_columns() -> None:
    result = _result()
    headers = [header for header, _ in CSV_COLUMNS]
    assert headers[-4:] == ["Site_No", "Site_Role", "Site_Primary_ID", "Site_Alternates"]
    path = Path(tempfile.mkdtemp()) / "c.csv"
    write_candidate_csv(path, result.candidates)
    with open(path, newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == len(result.candidates)
    assert {row["Site_Role"] for row in rows} <= {"PRIMARY", "ALTERNATE"}
    assert any(row["Site_Role"] == "PRIMARY" for row in rows)


def test_html_report_lists_sites_once() -> None:
    result = _result()
    path = Path(tempfile.mkdtemp()) / "r.html"
    write_html_report(path, result.candidates, result.metadata)
    text = path.read_text(encoding="utf-8")
    primaries = [c for c in result.candidates if c.site_role == "PRIMARY"]
    alternates = [c for c in result.candidates if c.site_role == "ALTERNATE"]
    assert "Map and ranked sites" in text
    for candidate in primaries[:3]:
        assert f"id='row-{candidate.candidate_id}'" in text
    for candidate in alternates[:5]:
        assert f"id='row-{candidate.candidate_id}'" not in text


def test_geodatabase_fields_and_map_layers_exist() -> None:
    source = (ROOT / "hlzcore" / "io_arcpy.py").read_text(encoding="utf-8")  # arcpy itself is not importable here
    for field in ('("Site_Role", "TEXT"', '("Site_Primary", "TEXT"', '("Site_Alts", "SHORT"'):
        assert field in source, field
    assert "Site_Role = 'PRIMARY'" in source and "Site_Role = 'ALTERNATE'" in source
