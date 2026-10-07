# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Human-readable outputs: CSV table, JSON metadata, HTML report, console table.

Design goals for v0.6.7 readability:

* The CSV has a single header row (v0.3 put ``#`` comment lines above the
  header, which breaks Excel and ArcGIS Pro table import). Licence and
  provenance are recorded in ``run_metadata.json`` instead.
* Candidates are sorted best-first with a rank and a short ID (HLZ-01 ...).
* Every candidate carries an MGRS reference and decimal latitude/longitude.
* The HTML report is a single self-contained file (no internet needed) with a
  colour legend, an overview map, a ranked table and an approach "rose" for
  each top candidate.
"""

from __future__ import annotations

import csv
import html
import json
import math
from pathlib import Path
from typing import Any, Iterable

from . import COPYRIGHT, LICENSE_ID, TOOL_NAME, __version__
from .geo import format_latlon
from .report_map import combined_css, map_block, map_payload
from .report_text import (
    EXTRA_CSS,
    approach_cell,
    column_help,
    howto_html,
    pilot_document,
    plain_summary,
    pilot_scripts,
    pilot_section,
    thead_html,
    viewbar_html,
)
from .models import (
    RATING_HIGHLY_SUITABLE,
    RATING_MARGINAL,
    RATING_ORDER,
    RATING_SUITABLE,
    RATING_UNASSESSED,
    RATING_UNSUITABLE,
    SECTOR_BLOCKED,
    SECTOR_CLEAR,
    SECTOR_INCOMPLETE,
    SECTOR_MARGINAL,
    Candidate,
)

RATING_COLOURS = {
    RATING_HIGHLY_SUITABLE: "#1a7f37",
    RATING_SUITABLE: "#5cb85c",
    RATING_MARGINAL: "#f0ad00",
    RATING_UNASSESSED: "#9e9e9e",
    RATING_UNSUITABLE: "#d32f2f",
}
RATING_RGB = {
    RATING_HIGHLY_SUITABLE: (26, 127, 55),
    RATING_SUITABLE: (92, 184, 92),
    RATING_MARGINAL: (240, 173, 0),
    RATING_UNASSESSED: (158, 158, 158),
    RATING_UNSUITABLE: (211, 47, 47),
}
SECTOR_COLOURS = {
    SECTOR_CLEAR: "#5cb85c",
    SECTOR_MARGINAL: "#f0ad00",
    SECTOR_BLOCKED: "#d32f2f",
    SECTOR_INCOMPLETE: "#bdbdbd",
}
SECTOR_RGB = {
    SECTOR_CLEAR: (92, 184, 92),
    SECTOR_MARGINAL: (240, 173, 0),
    SECTOR_BLOCKED: (211, 47, 47),
    SECTOR_INCOMPLETE: (189, 189, 189),
}

CSV_COLUMNS: list[tuple[str, str]] = [
    ("Rank", "rank"),
    ("HLZ_ID", "candidate_id"),
    ("Rating", "rating"),
    ("Score_0_100", "score"),
    ("Status", "status"),
    ("Confidence", "confidence"),
    ("MGRS", "mgrs"),
    ("Latitude", "latitude"),
    ("Longitude", "longitude"),
    ("X_projected", "x"),
    ("Y_projected", "y"),
    ("Elevation_m", "elevation_m"),
    ("Slope_deg", "slope_deg"),
    ("Aspect_downslope_deg", "aspect_deg"),
    ("Upslope_landing_only", "upslope_only"),
    ("Roughness_m", "roughness_m"),
    ("Max_obstacle_in_TDP_m", "max_tdp_obstacle_m"),
    ("Max_obstacle_in_ring_m", "max_ring_obstacle_m"),
    ("Distance_to_exclusion_m", "excluded_distance_m"),
    ("Nearest_obstacle_m", "nearest_obstacle_m"),
    ("Nearest_obstacle", "nearest_obstacle_text"),
    ("Distance_from_centre_m", "search_distance_m"),
    ("Bearing_from_centre_deg", "search_bearing_deg"),
    ("Approach_from_deg", "best_approach_azimuth_deg"),
    ("Landing_heading_deg", "landing_heading_deg"),
    ("Approach_obstacle_angle_deg", "best_approach_angle_deg"),
    ("Clear_sectors", "clear_sector_count"),
    ("Assessed_sectors", "assessed_sector_count"),
    ("Opposing_corridors", "opposing_corridors"),
    ("Approach_basis", "approach_basis"),
    ("Density_altitude_ft", "density_altitude_ft"),
    ("Main_limiting_factor", "limiting_factor"),
    ("Notes", "notes"),
]


def _fmt(value: Any, digits: int = 1) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        return f"{value:.{digits}f}"
    return str(value)


def _bearing(value: float | None) -> str:
    return "" if value is None else f"{int(round(value)) % 360:03d}"


def write_candidate_csv(path: str | Path, candidates: Iterable[Candidate]) -> Path:
    """Write a clean, Excel- and ArcGIS-friendly candidate table."""
    output = Path(path)
    with output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow([header for header, _ in CSV_COLUMNS])
        for candidate in candidates:
            data = candidate.to_dict()
            row = []
            for _header, key in CSV_COLUMNS:
                value = data.get(key)
                if key in {"latitude", "longitude"}:
                    row.append("" if value is None else f"{value:.7f}")
                elif key in {"x", "y"}:
                    row.append("" if value is None else f"{value:.2f}")
                elif isinstance(value, bool):
                    row.append("Yes" if value else "No")
                elif value is None:
                    row.append("")
                else:
                    row.append(value)
            writer.writerow(row)
    return output


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item") and callable(value.item):  # NumPy scalar
        return _json_safe(value.item())
    return value


def write_metadata(path: str | Path, metadata: dict[str, Any]) -> Path:
    output = Path(path)
    document = {
        "license": LICENSE_ID,
        "copyright": COPYRIGHT,
        "tool": TOOL_NAME,
        "tool_version": __version__,
        **metadata,
    }
    with output.open("w", encoding="utf-8") as stream:
        json.dump(_json_safe(document), stream, indent=2, sort_keys=False, allow_nan=False)
        stream.write("\n")
    return output


def console_summary(candidates: list[Candidate], limit: int = 15) -> list[str]:
    """Fixed-width summary table for the ArcGIS Pro geoprocessing messages."""
    lines = [
        "Rank  HLZ_ID   Rating            Score  Slope  Approach(from/angle)  MGRS                   Main limiting factor",
        "----  -------  ----------------  -----  -----  --------------------  ---------------------  --------------------",
    ]
    for candidate in candidates[:limit]:
        approach = (
            "-"
            if candidate.best_approach_azimuth_deg is None
            else f"{_bearing(candidate.best_approach_azimuth_deg)} / {_fmt(candidate.best_approach_angle_deg)} deg"
        )
        lines.append(
            f"{candidate.rank:>4}  {candidate.candidate_id:<7}  {candidate.rating:<16}  "
            f"{candidate.score:>5.1f}  {candidate.slope_deg:>4.1f}d  {approach:<20}  "
            f"{candidate.mgrs:<21}  {candidate.limiting_factor[:60]}"
        )
    if len(candidates) > limit:
        lines.append(f"... {len(candidates) - limit} more candidates in the CSV / HTML report")
    return lines


# --------------------------------------------------------------------------
# HTML report
# --------------------------------------------------------------------------


def _rose_svg(candidate: Candidate, size: int = 150, step_deg: float = 10.0) -> str:
    centre = size / 2.0
    radius = size / 2.0 - 14.0
    parts = [
        f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img" '
        f'aria-label="Approach sectors for {html.escape(candidate.candidate_id)}">'
    ]
    for sector in candidate.sectors:
        start = math.radians(sector.azimuth_deg - step_deg / 2.0)
        end = math.radians(sector.azimuth_deg + step_deg / 2.0)
        x1 = centre + radius * math.sin(start)
        y1 = centre - radius * math.cos(start)
        x2 = centre + radius * math.sin(end)
        y2 = centre - radius * math.cos(end)
        colour = SECTOR_COLOURS.get(sector.classification, "#bdbdbd")
        title = (
            f"From {_bearing(sector.azimuth_deg)}: {sector.classification}"
            + ("" if not math.isfinite(sector.worst_angle_deg) else f", {sector.worst_angle_deg:.1f} deg")
        )
        parts.append(
            f'<path d="M{centre:.1f},{centre:.1f} L{x1:.1f},{y1:.1f} A{radius:.1f},{radius:.1f} 0 0,1 '
            f'{x2:.1f},{y2:.1f} Z" fill="{colour}" stroke="#fff" stroke-width="0.6"><title>'
            f"{html.escape(title)}</title></path>"
        )
    if candidate.best_approach_azimuth_deg is not None:
        angle = math.radians(candidate.best_approach_azimuth_deg)
        x_tail = centre + (radius + 8) * math.sin(angle)
        y_tail = centre - (radius + 8) * math.cos(angle)
        parts.append(
            f'<line x1="{x_tail:.1f}" y1="{y_tail:.1f}" x2="{centre:.1f}" y2="{centre:.1f}" '
            'stroke="#0d47a1" stroke-width="3" marker-end="url(#arrow)"/>'
        )
    parts.append(
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" '
        'markerHeight="5" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#0d47a1"/>'
        "</marker></defs>"
    )
    parts.append(f'<circle cx="{centre}" cy="{centre}" r="5" fill="#212121"/>')
    parts.append(f'<text x="{centre}" y="10" text-anchor="middle" font-size="10" font-weight="bold">N</text>')
    parts.append("</svg>")
    return "".join(parts)


def _overview_svg(candidates: list[Candidate], extent: dict[str, float], width: int = 640) -> str:
    x_min, x_max = extent["x_min"], extent["x_max"]
    y_min, y_max = extent["y_min"], extent["y_max"]
    span_x = max(x_max - x_min, 1.0)
    span_y = max(y_max - y_min, 1.0)
    height = int(max(220, min(640, width * span_y / span_x)))
    pad = 20.0
    scale = min((width - 2 * pad) / span_x, (height - 2 * pad) / span_y)

    def project(x: float, y: float) -> tuple[float, float]:
        return pad + (x - x_min) * scale, height - pad - (y - y_min) * scale

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px" role="img" '
        'aria-label="Overview of candidate locations">',
        f'<rect x="{pad}" y="{pad}" width="{span_x * scale:.1f}" height="{span_y * scale:.1f}" '
        'fill="#f5f5f5" stroke="#757575" stroke-dasharray="4 3"/>',
    ]
    for candidate in reversed(candidates):
        px, py = project(candidate.x, candidate.y)
        colour = RATING_COLOURS.get(candidate.rating, "#9e9e9e")
        if candidate.best_approach_azimuth_deg is not None:
            angle = math.radians(candidate.best_approach_azimuth_deg)
            parts.append(
                f'<line x1="{px + 18 * math.sin(angle):.1f}" y1="{py - 18 * math.cos(angle):.1f}" '
                f'x2="{px:.1f}" y2="{py:.1f}" stroke="#0d47a1" stroke-width="1.5"/>'
            )
        parts.append(
            f'<circle cx="{px:.1f}" cy="{py:.1f}" r="6" fill="{colour}" stroke="#212121" stroke-width="0.8">'
            f"<title>{html.escape(candidate.candidate_id)} - {html.escape(candidate.rating)} "
            f"({candidate.score:.1f})</title></circle>"
        )
        if candidate.rank <= 15:
            parts.append(
                f'<text x="{px + 8:.1f}" y="{py - 8:.1f}" font-size="11" font-weight="bold">'
                f"{candidate.rank}</text>"
            )
    parts.append(
        f'<text x="{width - pad}" y="{pad - 6}" text-anchor="end" font-size="11" fill="#555">'
        "N up - blue tick = best approach direction</text>"
    )
    parts.append("</svg>")
    return "".join(parts)


def write_html_report(
    path: str | Path,
    candidates: list[Candidate],
    metadata: dict[str, Any],
    top_n: int = 10,
) -> Path:
    """Write a single-file, offline HTML report."""
    output = Path(path)
    esc = html.escape
    counts = {rating: 0 for rating in RATING_ORDER}
    for candidate in candidates:
        counts[candidate.rating] = counts.get(candidate.rating, 0) + 1
    profiles = metadata.get("profiles", {})
    aircraft = profiles.get("aircraft", {})
    doctrine = profiles.get("doctrine", {})
    inputs = metadata.get("inputs", {})
    search_meta = metadata.get("search") or {}
    has_search = bool(search_meta)
    search_line = f"<b>Search area:</b> {esc(str(search_meta.get('description', '')))}<br>\n" if has_search else ""
    grid = metadata.get("grid", {})
    results = metadata.get("results", {})
    options = metadata.get("options", {})
    warnings = metadata.get("warnings", [])

    cards = "".join(
        f'<div class="card" style="border-top:6px solid {RATING_COLOURS[rating]}">'
        f'<div class="num">{counts.get(rating, 0)}</div><div>{esc(rating)}</div></div>'
        for rating in RATING_ORDER
    )
    table_limit = 500
    rows = []
    for candidate in candidates[:table_limit]:
        colour = RATING_COLOURS.get(candidate.rating, "#9e9e9e")
        approach = approach_cell(candidate)
        rating_index = RATING_ORDER.index(candidate.rating) if candidate.rating in RATING_ORDER else 0
        if not has_search:
            centre_cell = ""
        elif candidate.search_distance_m is None:
            centre_cell = "<td></td>"
        else:
            centre_cell = f"<td class='r'>{candidate.search_distance_m:,.0f} m / {_bearing(candidate.search_bearing_deg)}&deg;</td>"
        rows.append(
            f"<tr id='row-{esc(candidate.candidate_id)}' class='r' data-rt='{rating_index}'>"
            f"<td class='c'>{candidate.rank}</td>"
            f"<td><a class='goto' href='#mapbox' data-id='{esc(candidate.candidate_id)}' title='Zoom to this site on the map'>{esc(candidate.candidate_id)}</a></td>"
            f"<td><span class='pill' style='background:{colour}'>{esc(candidate.rating)}</span></td>"
            f"<td class='r'>{candidate.score:.1f}</td>"
            f"<td class='mono'>{esc(candidate.mgrs)}</td>"
            f"{centre_cell}"
            f"<td class='r'>{candidate.slope_deg:.1f}&deg;{' &#8593;' if candidate.upslope_only else ''}</td>"
            f"<td class='r'>{candidate.roughness_m:.2f}</td>"
            f"<td class='r'>{_fmt(max(candidate.max_tdp_obstacle_m or 0.0, candidate.max_ring_obstacle_m or 0.0), 2) if candidate.max_tdp_obstacle_m is not None else 'n/a'}</td>"
            f"<td>{approach}</td>"
            f"<td class='c'>{candidate.clear_sector_count}/{candidate.assessed_sector_count}</td>"
            f"<td>{esc(candidate.limiting_factor)}</td>"
            "</tr>"
        )
    detail_cards = []
    for candidate in candidates[:top_n]:
        colour = RATING_COLOURS.get(candidate.rating, "#9e9e9e")
        da = "" if candidate.density_altitude_ft is None else f"<li>Density altitude: {candidate.density_altitude_ft:,.0f} ft</li>"
        if candidate.search_distance_m is not None:
            da += f"<li>From the search centre: {candidate.search_distance_m:,.0f} m, bearing {_bearing(candidate.search_bearing_deg)}&deg;</li>"
        if candidate.excluded_distance_m is not None:
            da += f"<li>Nearest exclusion area (e.g. water): {candidate.excluded_distance_m:.0f} m from the centre</li>"
        if candidate.nearest_obstacle_m is not None:
            da += f"<li>Nearest listed obstacle: {esc(candidate.nearest_obstacle_text or 'obstacle')}, {candidate.nearest_obstacle_m:.0f} m from the centre</li>"
        detail_cards.append(
            f"<div class='detail' style='border-left:8px solid {colour}'>"
            f"<div class='rose'>{_rose_svg(candidate)}</div>"
            f"<div><h3>#{candidate.rank} {esc(candidate.candidate_id)} &mdash; {esc(candidate.rating)} "
            f"({candidate.score:.1f})<button class='goto' type='button' data-id='{esc(candidate.candidate_id)}'>Show on map</button></h3><p class='plain'><b>In plain words:</b> {esc(plain_summary(candidate, aircraft))}</p><ul>"
            f"<li><b>MGRS:</b> <span class='mono'>{esc(candidate.mgrs) or 'n/a'}</span> &nbsp; "
            f"<b>Lat/Lon:</b> {esc(format_latlon(candidate.latitude, candidate.longitude)) or 'n/a'}</li>"
            f"<li>Elevation {candidate.elevation_m:,.1f} m; slope {candidate.slope_deg:.1f}&deg; "
            f"(downslope toward {_bearing(candidate.aspect_deg) or '-'}&deg;); roughness {candidate.roughness_m:.2f} m</li>"
            f"<li>Best approach: {('from ' + _bearing(candidate.best_approach_azimuth_deg) + '&deg;, landing heading ' + _bearing(candidate.landing_heading_deg) + '&deg;, obstacle angle ' + _fmt(candidate.best_approach_angle_deg) + '&deg;') if candidate.best_approach_azimuth_deg is not None else 'none'}"
            f"; clear sectors {candidate.clear_sector_count}/{candidate.assessed_sector_count}"
            f"{'; opposing corridors available' if candidate.opposing_corridors else ''}</li>"
            f"{da}"
            f"<li><b>Main limiting factor:</b> {esc(candidate.limiting_factor)}</li>"
            f"{'<li><b>Notes:</b> ' + esc(candidate.notes) + '</li>' if candidate.notes else ''}"
            f"<li>Component scores: slope {candidate.slope_component:.2f}, roughness {candidate.roughness_component:.2f}, "
            f"approach {candidate.approach_component:.2f}, obstacle {candidate.obstacle_component:.2f}, "
            f"data {candidate.data_quality_component:.2f}</li>"
            "</ul></div></div>"
        )
    warning_html = "".join(f"<li>{esc(str(item))}</li>" for item in warnings) or "<li>None</li>"
    limitations = "".join(f"<li>{esc(str(item))}</li>" for item in metadata.get("limitations", []))
    mission_profile = profiles.get("mission", {})
    columns = column_help(aircraft, mission_profile, doctrine, has_search)
    thead_markup = thead_html(columns)
    howto_markup = howto_html(columns, aircraft)
    pilot_html, pilot_json = pilot_section(candidates, aircraft, metadata, RATING_COLOURS, float(mission_profile.get("azimuth_step_deg", 10.0)))
    payload = map_payload(
        candidates, aircraft, float(profiles.get("mission", {}).get("azimuth_step_deg", 10.0)),
        RATING_ORDER, RATING_COLOURS, str(options.get("report_basemap", "ONLINE")), search_meta or None,
    )
    if payload and metadata.get("obstacles"):
        payload["obs"] = [
            [m["lat"], m["lon"], m["agl"], m.get("name", ""), m.get("kind", "point"), 1 if m.get("assumed") else 0]
            for m in metadata["obstacles"].get("markers", [])
        ]
    map_html, map_scripts = map_block(payload) if payload else ("", "")
    exclusion_value = str(inputs.get("exclusion") or "none")
    exclusion_text = (
        "none - water and wetlands are NOT excluded" if exclusion_value.lower() == "none" else exclusion_value
    )
    obstacle_info = metadata.get("obstacles") or {}
    obstacle_text = (
        str(inputs.get("obstacles") or "listed")
        + (f" ({obstacle_info.get('assumed_height_count', 0):,} with an ASSUMED height)" if obstacle_info.get("assumed_height_count") else "")
        if obstacle_info else "none - towers, masts and wires are NOT considered unless the surface model shows them"
    )
    overview = _overview_svg(candidates, metadata.get("candidate_extent") or grid_extent(grid)) if candidates else ""

    document = f"""<!DOCTYPE html>
<!-- SPDX-License-Identifier: {LICENSE_ID} -->
<!-- {COPYRIGHT} -->
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HLZ Report - {esc(str(metadata.get('run_name', '')))}</title>
<style>
:root {{ --ink:#212121; --muted:#616161; --line:#e0e0e0; }}
body {{ font-family: "Segoe UI", Arial, sans-serif; color:var(--ink); margin:24px auto; max-width:1200px; padding:0 16px; }}
h1 {{ margin-bottom:4px; }} h2 {{ border-bottom:2px solid var(--line); padding-bottom:4px; margin-top:28px; }}
.banner {{ background:#fff3cd; border:1px solid #f0ad00; padding:10px 14px; border-radius:6px; }}
.cards {{ display:flex; gap:12px; flex-wrap:wrap; margin:14px 0; }}
.card {{ background:#fafafa; border:1px solid var(--line); border-radius:6px; padding:10px 16px; min-width:120px; text-align:center; }}
.card .num {{ font-size:28px; font-weight:700; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th, td {{ border-bottom:1px solid var(--line); padding:6px 8px; vertical-align:top; }}
th {{ background:#f5f5f5; text-align:left; position:sticky; top:0; z-index:2; }}
td.r {{ text-align:right; }} td.c {{ text-align:center; }}
.mono {{ font-family: Consolas, "Courier New", monospace; white-space:nowrap; }}
.pill {{ color:#fff; padding:2px 8px; border-radius:10px; font-size:12px; white-space:nowrap; }}
.grid2 {{ display:grid; grid-template-columns: 1fr 1fr; gap:16px; }}
.box {{ border:1px solid var(--line); border-radius:6px; padding:10px 14px; font-size:13px; }}
.detail {{ display:flex; gap:16px; border:1px solid var(--line); border-radius:6px; padding:10px; margin:10px 0; }}
.detail h3 {{ margin:2px 0 6px 0; }} .detail ul {{ margin:0; padding-left:18px; font-size:13px; }}
.legend span {{ display:inline-block; margin-right:14px; font-size:13px; }}
.legend i {{ display:inline-block; width:12px; height:12px; margin-right:4px; vertical-align:middle; border-radius:2px; }}
.small {{ color:var(--muted); font-size:12px; }}
@media print {{ th {{ position:static; }} .detail {{ break-inside:avoid; }} }}
@media (max-width:800px) {{ .grid2 {{ grid-template-columns:1fr; }} .detail {{ flex-direction:column; }} }}
{combined_css() if payload else ''}
{EXTRA_CSS}
</style></head><body>
<h1>Helicopter Landing Zone Screening Report</h1>
<div class="small">{esc(TOOL_NAME)} {esc(__version__)} &middot; run "{esc(str(metadata.get('run_name', '')))}" &middot; {esc(str(metadata.get('run_started_utc', '')))} UTC</div>
<p class="banner"><b>Planning aid only.</b> Results do not certify a landing zone. Confirm every site by
ground or aerial reconnaissance and with the aircraft operator's performance data.
Profile status: <b>{esc(str(doctrine.get('approval_status', 'unknown')))}</b>.</p>

{viewbar_html()}
<div id="planner">
<h2>Summary</h2>
<div class="cards">{cards}</div>
<div class="legend"><b>Map colours:</b>
<span><i style="background:{RATING_COLOURS[RATING_HIGHLY_SUITABLE]}"></i>Highly suitable</span>
<span><i style="background:{RATING_COLOURS[RATING_SUITABLE]}"></i>Suitable</span>
<span><i style="background:{RATING_COLOURS[RATING_MARGINAL]}"></i>Marginal</span>
<span><i style="background:{RATING_COLOURS[RATING_UNSUITABLE]}"></i>Unsuitable</span>
<span><i style="background:{RATING_COLOURS[RATING_UNASSESSED]}"></i>Unassessed</span></div>

<h2>Map and ranked candidates</h2>
<section id="explore">
{map_html}
{howto_markup}
<table><thead>{thead_markup}</thead>
<tbody>{''.join(rows) or '<tr><td colspan="12">No candidates found.</td></tr>'}</tbody></table>
{f'<p class="small">Showing the best {table_limit} of {len(candidates):,} candidates. All candidates are in HLZ_Candidates.csv and the geodatabase.</p>' if len(candidates) > table_limit else ''}
</section>
<details><summary>Plain overview without a map (works in any viewer)</summary>{overview}</details>

<h2>Inputs and settings</h2>
<div class="grid2" style="margin-top:12px">
<div class="box"><b>Aircraft:</b> {esc(str(aircraft.get('display_name', '')))}<br>
TDP diameter {_fmt(aircraft.get('tdp_diameter_m'))} m + cleared ring {_fmt(aircraft.get('cleared_ring_m'))} m;
max slope {_fmt(aircraft.get('max_landing_slope_deg'))}&deg; (upslope {_fmt(aircraft.get('max_upslope_landing_deg'))}&deg;);
roughness &le; {_fmt(aircraft.get('max_roughness_m'), 2)} m;
approach clear &le; {_fmt(aircraft.get('suitable_angle_deg'))}&deg;, blocked &gt; {_fmt(aircraft.get('unsuitable_angle_deg'))}&deg;
over {_fmt(aircraft.get('approach_range_m'), 0)} m.<br>
<b>Doctrine:</b> {esc(str(doctrine.get('display_name', '')))} ({esc(str(doctrine.get('source_title', '')))})</div>
<div class="box"><b>DTM:</b> {esc(str(inputs.get('dtm', '')))}<br><b>DSM:</b> {esc(str(inputs.get('dsm') or 'none - obstacles NOT assessed'))}<br>
<b>Exclusion areas:</b> {esc(exclusion_text)}<br>
<b>Obstacles:</b> {esc(obstacle_text)}<br>
{search_line}Area {grid.get('width', '?'):,} x {grid.get('height', '?'):,} cells at {_fmt(metadata.get('cell_size_m'), 2)} m ({_fmt(metadata.get('area_km2'), 1)} km&sup2;, {metadata.get('tiling', {}).get('tile_count', 1)} tile(s));
ground-pass cells {results.get('ground_pass_cell_count', '?')}; candidates {len(candidates)}.<br>
OAT {_fmt(options.get('oat_c')) or 'not given'} C; altimeter {_fmt(options.get('altimeter_inhg'), 2)} inHg; hover mode {esc(str(options.get('performance_mode', '')))}.</div>
</div>

<h2>Top {min(top_n, len(candidates))} candidates in detail</h2>
<p class="legend"><b>Approach rose:</b>
<span><i style="background:{SECTOR_COLOURS[SECTOR_CLEAR]}"></i>Clear</span>
<span><i style="background:{SECTOR_COLOURS[SECTOR_MARGINAL]}"></i>Steep (marginal)</span>
<span><i style="background:{SECTOR_COLOURS[SECTOR_BLOCKED]}"></i>Blocked</span>
<span><i style="background:{SECTOR_COLOURS[SECTOR_INCOMPLETE]}"></i>No data</span>
&nbsp; blue arrow = recommended approach</p>
{''.join(detail_cards) or '<p>No candidates.</p>'}

<h2>Warnings</h2><ul>{warning_html}</ul>
<h2>Limitations</h2><ul>{limitations}</ul>
<p class="small">{esc(LICENSE_ID)} &middot; {esc(COPYRIGHT)}</p>
</div>
{pilot_html}
{map_scripts}
{pilot_scripts(pilot_json)}
</body></html>
"""
    output.write_text(document, encoding="utf-8")
    output.with_name("HLZ_Pilot_Brief.html").write_text(
        pilot_document("HLZ pilot brief", pilot_html, pilot_json), encoding="utf-8"
    )
    return output


def grid_extent(grid: dict[str, Any]) -> dict[str, float]:
    x_origin = float(grid.get("x_origin", 0.0))
    y_origin = float(grid.get("y_origin", 0.0))
    width = float(grid.get("width", 1)) * float(grid.get("pixel_width", 1.0))
    height = float(grid.get("height", 1)) * float(grid.get("pixel_height", -1.0))
    return {
        "x_min": min(x_origin, x_origin + width),
        "x_max": max(x_origin, x_origin + width),
        "y_min": min(y_origin, y_origin + height),
        "y_max": max(y_origin, y_origin + height),
    }


def rating_rgb(rating: str) -> tuple[int, int, int]:
    return RATING_RGB.get(rating, (158, 158, 158))


__all__ = [
    "CSV_COLUMNS",
    "RATING_COLOURS",
    "RATING_RGB",
    "SECTOR_COLOURS",
    "SECTOR_RGB",
    "console_summary",
    "grid_extent",
    "rating_rgb",
    "write_candidate_csv",
    "write_html_report",
    "write_metadata",
]

