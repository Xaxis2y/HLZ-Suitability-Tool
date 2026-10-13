# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Why were cells and candidates rejected? (v0.8.0)

Every analysed cell inside the area gets ONE reason: the first test it fails (see ``terrain.ground_reason_codes``;
exclusion areas and listed obstacles are applied afterwards and only take cells that otherwise pass). The per-tile
counts are plain integers stored with the tile checkpoint, so a resumed run gives the same summary.
"""

from __future__ import annotations

import csv
import html
from pathlib import Path
from typing import Any

import numpy as np

from .terrain import (
    REASON_KEYS,
    REASON_LABELS,
    REASON_PASS_ANY,
    REASON_PASS_UPSLOPE,
)

ORDERED_CODES = list(REASON_KEYS)  # 0..9 in test order


def tile_counts(reason: np.ndarray, inside: np.ndarray) -> dict[str, int]:
    """Counts per reason key for the cells where ``inside`` is True (tile x AOI x valid DTM)."""
    selected = reason[inside]
    counts = np.bincount(selected, minlength=256) if selected.size else np.zeros(256, dtype=np.int64)
    return {REASON_KEYS[code]: int(counts[code]) for code in ORDERED_CODES}


def build_summary(
    cell_counts: dict[str, int],
    candidate_numbers: dict[str, int],
    cell_size_m: float,
) -> dict[str, Any]:
    """Assemble the run-level summary from the summed per-tile counts."""
    evaluated = sum(cell_counts.values())
    area_per_cell_ha = cell_size_m * cell_size_m / 1.0e4
    rows = []
    for code in ORDERED_CODES:
        key = REASON_KEYS[code]
        cells = int(cell_counts.get(key, 0))
        rows.append({
            "key": key,
            "label": REASON_LABELS[code],
            "passed": code in (REASON_PASS_ANY, REASON_PASS_UPSLOPE),
            "cells": cells,
            "percent": round(100.0 * cells / evaluated, 2) if evaluated else 0.0,
            "area_ha": round(cells * area_per_cell_ha, 2),
        })
    failed = [row for row in rows if not row["passed"] and row["cells"]]
    top = max(failed, key=lambda row: row["cells"]) if failed else None
    return {
        "cells_evaluated": evaluated,
        "cell_size_m": cell_size_m,
        "reasons": rows,
        "main_reason": None if top is None else {"key": top["key"], "label": top["label"], "percent": top["percent"]},
        "candidates": dict(candidate_numbers),
    }


def summary_lines(summary: dict[str, Any]) -> list[str]:
    """Short plain-text lines for the run log (only reasons that occurred)."""
    evaluated = summary["cells_evaluated"]
    if not evaluated:
        return ["Cell results: no cell was evaluated"]
    lines = [f"Cell results ({evaluated:,} cells evaluated; each cell counted once, under the first test it fails):"]
    for row in summary["reasons"]:
        if row["cells"]:
            lines.append(f"  {row['percent']:6.2f}%  {row['cells']:>12,}  {row['label']}")
    c = summary["candidates"]
    lines.append(
        "Candidate results: "
        f"{c.get('exact_checks', 0):,} exact checks, {c.get('exact_rejected', 0):,} rejected; "
        f"{c.get('seam_duplicates_removed', 0):,} tile-seam duplicates removed; "
        + (f"{c.get('dropped_by_spacing', 0):,} dropped for the minimum spacing; " if c.get("dropped_by_spacing") else "")
        + f"{c.get('dropped_by_limit', 0):,} dropped by the candidate limit; {c.get('kept', 0):,} kept"
    )
    return lines


def write_csv(path: str | Path, summary: dict[str, Any]) -> Path:
    output = Path(path)
    with output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["Reason", "Passed_ground_test", "Cells", "Percent_of_cells", "Area_ha"])
        for row in summary["reasons"]:
            writer.writerow([row["label"], "Yes" if row["passed"] else "No", row["cells"], f"{row['percent']:.2f}", f"{row['area_ha']:.2f}"])
        writer.writerow([])
        writer.writerow(["Candidate step", "Count"])
        c = summary["candidates"]
        for label, key in (
            ("Exact checks of screened sites", "exact_checks"),
            ("Rejected by the exact check", "exact_rejected"),
            ("Tile-seam duplicates removed", "seam_duplicates_removed"),
            ("Dropped for the minimum spacing", "dropped_by_spacing"),
            ("Dropped by the candidate limit", "dropped_by_limit"),
            ("Candidates kept", "kept"),
        ):
            writer.writerow([label, c.get(key, 0)])
    return output


def html_section(summary: dict[str, Any] | None) -> str:
    """HTML block for the report: a bar per reason."""
    if not summary or not summary.get("cells_evaluated"):
        return ""
    esc = html.escape
    rows = []
    for row in summary["reasons"]:
        if not row["cells"]:
            continue
        colour = "#5cb85c" if row["passed"] else "#d9822b"
        width = max(0.5, min(100.0, row["percent"]))
        rows.append(
            f"<tr><td>{esc(row['label'])}</td><td class='r'>{row['cells']:,}</td><td class='r'>{row['percent']:.2f}%</td>"
            f"<td style='width:34%'><div style='background:{colour};height:10px;width:{width:.1f}%'></div></td></tr>"
        )
    c = summary["candidates"]
    main = summary.get("main_reason")
    lead = (
        f"<p>The most common reason a cell did not pass: <b>{esc(main['label'].lower())}</b> ({main['percent']:.1f}% of the cells).</p>"
        if main else "<p>Every evaluated cell passed the ground test.</p>"
    )
    return (
        "<h2>Why cells and sites were rejected</h2>" + lead +
        "<table><thead><tr><th>Result of the ground test</th><th class='r'>Cells</th><th class='r'>Share</th><th></th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table>"
        "<p class='small'>Each cell is counted once, under the first test it fails, in the order listed in the CSV "
        "<code>HLZ_Rejection_Summary.csv</code>.</p>"
        f"<p class='small'>Candidates: {c.get('exact_checks', 0):,} screened sites were checked exactly and "
        f"{c.get('exact_rejected', 0):,} rejected; {c.get('seam_duplicates_removed', 0):,} tile-seam duplicates removed; "
        + (f"{c.get('dropped_by_spacing', 0):,} dropped for the minimum spacing; " if c.get("dropped_by_spacing") else "")
        + f"{c.get('dropped_by_limit', 0):,} dropped by the candidate limit; {c.get('kept', 0):,} kept.</p>"
    )
