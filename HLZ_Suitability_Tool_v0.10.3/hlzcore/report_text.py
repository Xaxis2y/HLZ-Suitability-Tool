# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Plain-language text and the pilot brief for HLZ_Report.html (v0.6.7).

Two audiences read this report:

* the **planner**, who may know nothing about helicopters. Every column heading gets a "?" with what the
  column means and why it matters, a "How to read this table" panel, a plain sentence for every site, and
  a "Best approach" cell that spells out what ``from 100 (land 280), 1.2`` means;
* the **pilot**, who gets a separate view (a button in the report, or ``HLZ_Pilot_Brief.html``): one
  card per site with position, how to come in, the highest obstacle, which directions are open, a small
  rose, a wind helper and the cautions. Bearings are TRUE; heights are in feet, as pilots use them.
"""

from __future__ import annotations

import html
import json
import math
import re
from typing import Any

from .models import SECTOR_BLOCKED, SECTOR_CLEAR, SECTOR_INCOMPLETE, SECTOR_MARGINAL, Candidate

COMPASS_16 = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
COMPASS_WORDS = {
    "N": "north", "NNE": "north-north-east", "NE": "north-east", "ENE": "east-north-east", "E": "east",
    "ESE": "east-south-east", "SE": "south-east", "SSE": "south-south-east", "S": "south", "SSW": "south-south-west",
    "SW": "south-west", "WSW": "west-south-west", "W": "west", "WNW": "west-north-west", "NW": "north-west", "NNW": "north-north-west",
}
FEET_PER_METRE = 3.280839895
PILOT_SITE_LIMIT = 30
SECTOR_FILL = {SECTOR_CLEAR: "#5cb85c", SECTOR_MARGINAL: "#f0ad00", SECTOR_BLOCKED: "#d32f2f", SECTOR_INCOMPLETE: "#bdbdbd"}
SECTOR_LETTER = {SECTOR_CLEAR: "C", SECTOR_MARGINAL: "M", SECTOR_BLOCKED: "B", SECTOR_INCOMPLETE: "I"}
_esc = html.escape


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def compass(bearing: float | None) -> str:
    """16-point compass name of a bearing in degrees (``ESE``)."""
    if bearing is None:
        return ""
    return COMPASS_16[int(round((float(bearing) % 360.0) / 22.5)) % 16]


def compass_words(bearing: float | None) -> str:
    return COMPASS_WORDS.get(compass(bearing), "") if bearing is not None else ""


def b3(value: float | None) -> str:
    """Bearing as three digits (``075``)."""
    return "" if value is None else f"{int(round(float(value))) % 360:03d}"


def dms(latitude: float | None, longitude: float | None) -> str:
    """Latitude/longitude as degrees, minutes, seconds (``45°25′48″N 075°41′30″W``)."""
    if latitude is None or longitude is None:
        return ""

    def one(value: float, positive: str, negative: str, width: int) -> str:
        hemisphere = positive if value >= 0 else negative
        total = round(abs(value) * 3600.0)
        degrees, rest = divmod(int(total), 3600)
        minutes, seconds = divmod(rest, 60)
        return f"{degrees:0{width}d}\u00b0{minutes:02d}\u2032{seconds:02d}\u2033{hemisphere}"

    return f"{one(latitude, 'N', 'S', 2)} {one(longitude, 'E', 'W', 3)}"


def sector_ranges(candidate: Candidate, classification: str, step_deg: float) -> list[str]:
    """Merged bearing ranges (``030°-090°``) of the sectors of one class; ``['all directions']`` if complete."""
    azimuths = sorted(s.azimuth_deg for s in candidate.sectors if s.classification == classification)
    if not azimuths:
        return []
    total = len(candidate.sectors)
    if len(azimuths) == total:
        return ["all directions"]
    runs: list[list[float]] = []
    for azimuth in azimuths:
        if runs and abs(azimuth - runs[-1][-1] - step_deg) < 1.0e-6:
            runs[-1].append(azimuth)
        else:
            runs.append([azimuth])
    if len(runs) > 1 and abs((runs[0][0] + 360.0) - runs[-1][-1] - step_deg) < 1.0e-6:
        runs[0] = runs.pop()[:] + runs[0]  # a run that wraps through north
    texts = []
    for run in runs:
        low = (run[0] - step_deg / 2.0) % 360.0
        high = (run[-1] + step_deg / 2.0) % 360.0
        texts.append(f"{b3(low)}\u00b0\u2013{b3(high)}\u00b0")
    return texts


def _best_sector(candidate: Candidate):  # type: ignore[no-untyped-def]
    if candidate.best_approach_azimuth_deg is None:
        return None
    return next((s for s in candidate.sectors if abs(s.azimuth_deg - candidate.best_approach_azimuth_deg) < 1.0e-6), None)


# ---------------------------------------------------------------------------
# One plain sentence per site
# ---------------------------------------------------------------------------


def plain_summary(candidate: Candidate, aircraft: dict[str, Any]) -> str:
    tdp = float(aircraft.get("tdp_diameter_m", 50.0))
    ring = float(aircraft.get("cleared_ring_m", 20.0))
    max_slope = float(aircraft.get("max_landing_slope_deg", 7.0))
    suitable = float(aircraft.get("suitable_angle_deg", 3.0))
    slope = candidate.slope_deg
    parts: list[str] = []
    if candidate.upslope_only:
        uphill = None if candidate.aspect_deg is None else (candidate.aspect_deg + 180.0) % 360.0
        where = f" (heading {b3(uphill)}\u00b0)" if uphill is not None else ""
        parts.append(f"The ground is steep ({slope:.1f}\u00b0), so the helicopter can only land facing uphill{where}")
    elif slope < 1.0:
        parts.append(f"The ground is almost level ({slope:.1f}\u00b0)")
    elif slope < 3.0:
        parts.append(f"The ground slopes gently ({slope:.1f}\u00b0)")
    else:
        parts.append(f"The ground slopes ({slope:.1f}\u00b0, within the {max_slope:.0f}\u00b0 limit)")
    limit = float(aircraft.get("max_roughness_m", 0.3))
    if candidate.roughness_m < 0.05:
        parts[-1] += " and smooth"
    elif candidate.roughness_m < 0.5 * limit:
        parts[-1] += " and fairly smooth"
    else:
        parts[-1] += f" and uneven ({candidate.roughness_m:.2f} m of bumps)"
    obstacle_clear = False
    if candidate.max_tdp_obstacle_m is None:
        obstacle_text = "Trees and buildings were NOT checked (no surface model was given), so look at recent imagery before trusting this site"
    else:
        tallest = max(candidate.max_tdp_obstacle_m or 0.0, candidate.max_ring_obstacle_m or 0.0)
        if tallest < 0.05:
            obstacle_clear = True
            obstacle_text = f"nothing stands in the {tdp:.0f} m landing circle or the {ring:.0f} m ring around it"
        else:
            obstacle_text = f"The tallest object near the pad is {tallest:.1f} m"
    sentence = f"{parts[0]}, and {obstacle_text}." if obstacle_clear else f"{parts[0]}. {obstacle_text}."
    if candidate.best_approach_azimuth_deg is None:
        sentence += " No approach direction is clear enough to use."
    else:
        sentence += (
            f" {candidate.clear_sector_count} of {candidate.assessed_sector_count} approach directions are open. The best way in is from "
            f"the {compass_words(candidate.best_approach_azimuth_deg)} ({b3(candidate.best_approach_azimuth_deg)}\u00b0), flying toward the "
            f"{compass_words(candidate.landing_heading_deg)} ({b3(candidate.landing_heading_deg)}\u00b0)."
        )
        angle = candidate.best_approach_angle_deg
        if angle is not None:
            if angle <= 0.0:
                sentence += " Looking along that path from the pad, nothing rises above the horizon."
            elif angle <= suitable:
                sentence += f" The highest obstacle on that path is only {angle:.1f}\u00b0 above the horizon (up to {suitable:.0f}\u00b0 counts as clear)."
            else:
                sentence += f" The highest obstacle on that path is {angle:.1f}\u00b0 above the horizon, so a steep approach is needed."
    if candidate.search_distance_m is not None:
        sentence += f" It is {candidate.search_distance_m:,.0f} m {compass_words(candidate.search_bearing_deg)} of your search centre."
    if candidate.nearest_obstacle_m is not None and candidate.nearest_obstacle_m <= 3.0 * (tdp / 2.0 + ring):
        sentence += f" A listed obstacle ({candidate.nearest_obstacle_text or 'obstacle'}) is {candidate.nearest_obstacle_m:.0f} m away."
    if candidate.excluded_distance_m is not None and candidate.excluded_distance_m <= 3.0 * (tdp / 2.0 + ring):
        sentence += f" An excluded area (for example water) is {candidate.excluded_distance_m:.0f} m away."
    sentence += f" Overall: {candidate.rating} (score {candidate.score:.0f} out of 100)."
    if candidate.limiting_factor and not candidate.limiting_factor.startswith("None"):
        sentence += f" Weakest point: {candidate.limiting_factor}."
    return sentence


# ---------------------------------------------------------------------------
# Column explanations
# ---------------------------------------------------------------------------


def column_help(aircraft: dict[str, Any], mission: dict[str, Any], doctrine: dict[str, Any], has_search: bool) -> list[dict[str, str]]:
    tdp = float(aircraft.get("tdp_diameter_m", 50.0))
    ring = float(aircraft.get("cleared_ring_m", 20.0))
    max_slope = float(aircraft.get("max_landing_slope_deg", 7.0))
    up_slope = float(aircraft.get("max_upslope_landing_deg", 15.0))
    rough = float(aircraft.get("max_roughness_m", 0.3))
    obs_tdp = float(aircraft.get("tdp_obstacle_height_m", 0.3))
    obs_ring = float(aircraft.get("ring_obstacle_height_m", 1.0))
    suitable = float(aircraft.get("suitable_angle_deg", 3.0))
    blocked = float(aircraft.get("unsuitable_angle_deg", 7.0))
    clearance = float(aircraft.get("clearance_height_m", 2.0))
    reach = float(aircraft.get("approach_range_m", 1000.0))
    weights = {k: float(mission.get(f"{k}_weight", 0.0)) for k in ("slope", "roughness", "approach", "obstacle", "data_quality")}
    total = sum(weights.values()) or 1.0
    percent = {k: f"{100.0 * v / total:.0f}%" for k, v in weights.items()}
    high = float(doctrine.get("highly_suitable_score", 80.0))
    columns = [
        dict(key="rank", head="#", what="The site's position in the list, best first. Sites are ordered by rating, then by score.",
             good="Start at the top: the list is already sorted.", why="You do not have to compare every site yourself."),
        dict(key="id", head="ID", what="A short label for the site (HLZ-01, HLZ-02, ...). Click it and the map zooms to the site.",
             good="", why="It gives everyone the same name for the same place."),
        dict(key="rating", head="Rating", what="The overall verdict: Highly suitable, Suitable, Marginal, Unassessed or Unsuitable.",
             good="Highly suitable or Suitable.",
             why="A site that fails a safety test can never be rated well, however good its other numbers are."),
        dict(key="score", head="Score",
             what=(f"A number from 0 to 100 that blends five checks: approach ({percent['approach']}), slope ({percent['slope']}), roughness "
                   f"({percent['roughness']}), obstacles ({percent['obstacle']}) and data quality ({percent['data_quality']})."),
             good=f"Higher is better. {high:.0f} or more is Highly suitable.",
             why="It lets you compare sites at a glance. But a high score can hide one serious weakness, so always read the last column too."),
        dict(key="mgrs", head="MGRS", what="The site's military grid reference, to the nearest metre.", good="",
             why="This is how you find the site on a map or enter it in navigation equipment."),
    ]
    if has_search:
        columns.append(dict(key="centre", head="From centre",
                            what="How far the site is from the search centre you chose, and in which compass direction (1,240 m / 075° is 1,240 m to the east-north-east).",
                            good="Smaller is nearer.", why="Among equally good sites, the nearer one is usually the one you want."))
    columns += [
        dict(key="slope", head="Slope",
             what="How steep the ground is under the landing circle, in degrees (0° is flat). An arrow (↑) means the site is only usable if the aircraft lands facing uphill.",
             good=f"Under {max_slope:.0f}° for any landing direction; up to {up_slope:.0f}° only when landing facing uphill.",
             why="On a slope a helicopter can slide or tip over, the cabin tilts, and the rotor can come close to the high side of the ground."),
        dict(key="rough", head="Rough (m)",
             what="How bumpy the ground is: the biggest gap between the real ground and a flat plane laid across the landing circle, in metres.",
             good=f"Under {rough:.2f} m.",
             why="Rocks, ditches, stumps and furrows can damage skids, wheels or the underside, and make the aircraft rock as it touches down."),
        dict(key="obstacle", head="Obstacle (m)",
             what="The height of the tallest thing found standing on the ground in the landing circle or the ring around it (a bush, tree, pole, building, vehicle). 'n/a' means no surface model was given.",
             good=f"No more than {obs_tdp:.1f} m inside the {tdp:.0f} m landing circle, and {obs_ring:.1f} m in the {ring:.0f} m ring around it.",
             why="Rotor blades and the tail reach well beyond the landing spot, and the rotor wash throws up debris. Anything tall near the pad is a strike risk."),
        dict(key="approach", head="Best approach",
             what=(f"The best direction to come in from. Example: 'from 100° (ESE)' means the aircraft arrives from the east-south-east of the site. "
                   f"'Land on 280° (WNW)' is the compass heading it flies as it lands (the opposite way). 'Obstacle angle 1.2°' is how high the tallest obstacle "
                   f"on that path rises above the horizon, seen from {clearance:.0f} m above the pad, within {reach:.0f} m. A negative number means nothing rises above the horizon."),
             good=f"{suitable:.0f}° or less is clear; over {blocked:.0f}° is blocked; in between needs a steep approach.",
             why="A helicopter comes down along a slanted path. Trees, buildings or hills that are too high along it make that approach unsafe. The direction also decides how the wind meets the aircraft."),
        dict(key="clear", head="Clear",
             what="How many of the approach directions that were checked (one every 10°, 36 in all) have a clear way in. '29/36' means 29 of 36.",
             good="The more the better.",
             why="With many open directions the pilot can always land into the wind and keep an escape route. A site with one open direction is fragile."),
        dict(key="limiting", head="Main limiting factor",
             what="The weakest point of this site, in words.", good="'None - all criteria well within limits' is best.",
             why="It tells you what to check first on the ground or on imagery."),
    ]
    return columns


def thead_html(columns: list[dict[str, str]]) -> str:
    cells = []
    for index, col in enumerate(columns):
        right = " r" if index >= len(columns) - 5 else ""
        box = f"<b>{_esc(col['head'])}</b> {_esc(col['what'])}"
        if col["good"]:
            box += f"<br><em>Good:</em> {_esc(col['good'])}"
        box += f"<br><em>Why it matters:</em> {_esc(col['why'])}"
        tip = f'<span class="tip{right}" tabindex="0" role="note" aria-label="{_esc(col["head"])}: {_esc(col["what"])}"><i>?</i><span class="tipbox">{box}</span></span>'
        cells.append(f"<th>{_esc(col['head'])}{tip}</th>")
    return "<tr>" + "".join(cells) + "</tr>"


def howto_html(columns: list[dict[str, str]], aircraft: dict[str, Any]) -> str:
    tdp = float(aircraft.get("tdp_diameter_m", 50.0))
    ring = float(aircraft.get("cleared_ring_m", 20.0))
    rows = []
    for col in columns:
        good = f"<br><span class='good'>Good: {_esc(col['good'])}</span>" if col["good"] else ""
        rows.append(f"<tr><th scope='row'>{_esc(col['head'])}</th><td>{_esc(col['what'])}{good}</td><td>{_esc(col['why'])}</td></tr>")
    words = [
        ("Landing zone (LZ)", "A place where a helicopter can land."),
        ("Landing circle", f"The circle where the aircraft sets its wheels or skids down, {tdp:.0f} m across for the aircraft chosen. It must be flat, smooth and free of obstacles."),
        ("Cleared ring", f"The band {ring:.0f} m wide around the landing circle. It is checked for obstacles too, because rotors and tail reach out beyond the landing spot."),
        ("Bearing", "A compass direction in degrees from true north: 0° north, 90° east, 180° south, 270° west. Every bearing in this report is TRUE, not magnetic."),
        ("Approach", "The path the aircraft flies down to the landing circle. 'From 100°' names where it starts, as seen from the site."),
        ("Obstacle angle", "How high an obstacle looks above the horizon from the pad, in degrees. The bigger the angle, the harder it is to fly over it and still reach the pad."),
    ]
    glossary = "".join(f"<dt>{_esc(a)}</dt><dd>{_esc(b)}</dd>" for a, b in words)
    return (
        '<details id="howto" open><summary>How to read this table (what each column means and why it matters)</summary>'
        '<p class="small">Hover over or tap the <b>?</b> next to any column heading for the same explanation.</p>'
        '<table class="howto"><thead><tr><th>Column</th><th>What it is</th><th>Why it matters</th></tr></thead><tbody>'
        + "".join(rows)
        + f"</tbody></table><h4>Words used in this report</h4><dl class='gloss'>{glossary}</dl></details>"
    )


def approach_cell(candidate: Candidate) -> str:
    if candidate.best_approach_azimuth_deg is None:
        return "<span class='small'>none usable</span>"
    angle = candidate.best_approach_angle_deg
    angle_text = "nothing above the horizon" if angle is not None and angle <= 0 else f"obstacle angle {angle:.1f}&deg;"
    return (
        f"<b>from {b3(candidate.best_approach_azimuth_deg)}&deg; {compass(candidate.best_approach_azimuth_deg)}</b><br>"
        f"<span class='small'>land on {b3(candidate.landing_heading_deg)}&deg; {compass(candidate.landing_heading_deg)}, {angle_text}</span>"
    )


# ---------------------------------------------------------------------------
# Pilot brief
# ---------------------------------------------------------------------------


def _pilot_rose(candidate: Candidate, step_deg: float, size: int = 168) -> str:
    centre = size / 2.0
    radius = size / 2.0 - 24.0
    ident = re.sub(r"[^A-Za-z0-9]", "", candidate.candidate_id)
    parts = [f'<svg class="prose" viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img" aria-label="Approach rose for {_esc(candidate.candidate_id)}">']
    for sector in candidate.sectors:
        a1 = math.radians(sector.azimuth_deg - step_deg / 2.0)
        a2 = math.radians(sector.azimuth_deg + step_deg / 2.0)
        x1, y1 = centre + radius * math.sin(a1), centre - radius * math.cos(a1)
        x2, y2 = centre + radius * math.sin(a2), centre - radius * math.cos(a2)
        parts.append(
            f'<path d="M{centre:.1f},{centre:.1f} L{x1:.1f},{y1:.1f} A{radius:.1f},{radius:.1f} 0 0,1 {x2:.1f},{y2:.1f} Z" '
            f'fill="{SECTOR_FILL.get(sector.classification, "#bdbdbd")}" stroke="#fff" stroke-width="0.6"/>'
        )
    for letter, bearing in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
        x = centre + (radius + 13) * math.sin(math.radians(bearing))
        y = centre - (radius + 13) * math.cos(math.radians(bearing)) + 4
        parts.append(f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="middle" font-size="12" font-weight="700" fill="#212121">{letter}</text>')
    if candidate.best_approach_azimuth_deg is not None:
        angle = math.radians(candidate.best_approach_azimuth_deg)
        parts.append(
            f'<defs><marker id="pa{ident}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="#0d47a1"/></marker></defs>'
            f'<line x1="{centre + (radius + 4) * math.sin(angle):.1f}" y1="{centre - (radius + 4) * math.cos(angle):.1f}" '
            f'x2="{centre:.1f}" y2="{centre:.1f}" stroke="#0d47a1" stroke-width="3" marker-end="url(#pa{ident})"/>'
        )
    parts.append(f'<circle cx="{centre}" cy="{centre}" r="4" fill="#212121"/></svg>')
    return "".join(parts)


def pilot_card(candidate: Candidate, aircraft: dict[str, Any], step_deg: float, rating_colour: str, total: int, status_text: str, obstacles_listed: bool = False) -> str:
    tdp = float(aircraft.get("tdp_diameter_m", 50.0))
    ring = float(aircraft.get("cleared_ring_m", 20.0))
    esc = _esc
    position = (
        f"<p class='pmgrs'>{esc(candidate.mgrs)}</p><p>{esc(dms(candidate.latitude, candidate.longitude))}<br>"
        f"<span class='small'>{candidate.latitude:.5f}, {candidate.longitude:.5f} (WGS84)</span></p>"
        if candidate.latitude is not None and candidate.mgrs
        else f"<p class='pmgrs'>E {candidate.x:,.0f}  N {candidate.y:,.0f}</p><p class='small'>projected coordinates of the elevation data</p>"
    )
    elevation = (
        f"<p>Ground elevation <b>{candidate.elevation_m * FEET_PER_METRE:,.0f} ft</b> ({candidate.elevation_m:,.1f} m), "
        "from the terrain model (close to sea level height).</p>"
    )
    if candidate.density_altitude_ft is not None:
        elevation += f"<p>Density altitude <b>{candidate.density_altitude_ft:,.0f} ft</b> at the temperature and pressure entered.</p>"
    # the landing area
    if candidate.upslope_only:
        uphill = None if candidate.aspect_deg is None else (candidate.aspect_deg + 180.0) % 360.0
        slope_line = f"<p class='pwarn'>Slope {candidate.slope_deg:.1f}&deg;: LAND FACING UPHILL (heading {b3(uphill)}&deg;).</p>"
    else:
        down = f", downhill toward {b3(candidate.aspect_deg)}&deg;" if candidate.aspect_deg is not None and candidate.slope_deg >= 1.0 else ""
        slope_line = f"<p>Slope <b>{candidate.slope_deg:.1f}&deg;</b>{down}. Ground roughness {candidate.roughness_m:.2f} m.</p>"
    if candidate.max_tdp_obstacle_m is None:
        obstacle_line = "<p class='pwarn'>Trees and buildings were NOT checked (no surface model).</p>"
    else:
        tallest = max(candidate.max_tdp_obstacle_m or 0.0, candidate.max_ring_obstacle_m or 0.0)
        obstacle_line = f"<p>Tallest object in the circle and ring: <b>{tallest:.1f} m</b> ({tallest * FEET_PER_METRE:.0f} ft).</p>"
    area = (
        f"<h4>The landing area</h4><p>Landing circle <b>{tdp:.0f} m</b> ({tdp * FEET_PER_METRE:.0f} ft) across, inside a {ring:.0f} m "
        f"({ring * FEET_PER_METRE:.0f} ft) ring that was also checked.</p>{slope_line}{obstacle_line}"
    )
    # how to come in
    sector = _best_sector(candidate)
    if candidate.best_approach_azimuth_deg is None:
        way = "<p class='pbig pwarn'>No approach direction is clear enough to use.</p>"
    else:
        way = (
            f"<p class='pbig'>Come in from {b3(candidate.best_approach_azimuth_deg)}&deg; ({compass(candidate.best_approach_azimuth_deg)}).<br>"
            f"Land on heading {b3(candidate.landing_heading_deg)}&deg; ({compass(candidate.landing_heading_deg)}).</p>"
        )
        if sector is not None and sector.worst_height_above_lz_m is not None and sector.worst_distance_m is not None:
            height_m = sector.worst_height_above_lz_m
            bearing = None
            if sector.worst_x is not None and sector.worst_y is not None:
                bearing = (math.degrees(math.atan2(sector.worst_x - candidate.x, sector.worst_y - candidate.y)) + 360.0) % 360.0
            where = f", bearing {b3(bearing)}&deg;" if bearing is not None else ""
            if height_m <= 0.0:
                way += "<p>Nothing along this approach rises above the height of the pad.</p>"
            else:
                way += (
                    f"<p>Highest obstacle on this approach: <b>{height_m * FEET_PER_METRE:,.0f} ft</b> ({height_m:,.0f} m) above the pad, "
                    f"{sector.worst_distance_m:,.0f} m ({sector.worst_distance_m / 1852.0:.2f} NM) out{where}.</p>"
                )
                if sector.worst_obstacle:
                    way += f"<p class='pwarn'>This is a LISTED obstacle: {esc(sector.worst_obstacle)}.</p>"
    lines = []
    for label, cls in (("Clear", SECTOR_CLEAR), ("Steep (needs a steep approach)", SECTOR_MARGINAL), ("Blocked", SECTOR_BLOCKED), ("No data", SECTOR_INCOMPLETE)):
        ranges = sector_ranges(candidate, cls, step_deg)
        if ranges:
            lines.append(f"<tr><th><i style='background:{SECTOR_FILL[cls]}'></i>{label}</th><td>{esc(', '.join(ranges))}</td></tr>")
    directions = (
        "<table class='pdir'><caption>Directions you can come in FROM (true bearings, read clockwise)</caption><tbody>" + "".join(lines) + "</tbody></table>"
        if lines else ""
    )
    wind = f"<p class='windres' data-for='{esc(candidate.candidate_id)}'>Enter the wind above to see the best approach for it.</p>"
    # cautions
    cautions = [
        (
            "Towers, masts and wires were checked only where they are in the obstacle list used; anything missing from it was not seen. Not checked by this tool: how firm the ground is, snow or dust, vehicles and people, wind and airspace. Check from the air and on the ground before landing."
            if obstacles_listed else
            "Not checked by this tool: towers, masts, wires and poles (no obstacle list was used), how firm the ground is, snow or dust, vehicles and people, wind and airspace. Check from the air and on the ground before landing."
        ),
        "All bearings are TRUE north. Apply the local magnetic variation.",
    ]
    if candidate.confidence != "HIGH":
        cautions.insert(0, f"Data confidence is {candidate.confidence}: the terrain data are coarse or there is no surface model.")
    if candidate.nearest_obstacle_m is not None and candidate.nearest_obstacle_m <= 3.0 * (tdp / 2.0 + ring):
        cautions.insert(0, f"A listed obstacle ({candidate.nearest_obstacle_text or 'obstacle'}) is {candidate.nearest_obstacle_m:.0f} m from the pad.")
    if candidate.excluded_distance_m is not None and candidate.excluded_distance_m <= 3.0 * (tdp / 2.0 + ring):
        cautions.insert(0, f"An excluded area (for example water) is {candidate.excluded_distance_m:.0f} m from the pad.")
    if candidate.notes:
        cautions.append(candidate.notes)
    cautions.append(status_text)
    centre = (
        f"<p class='small'>{candidate.search_distance_m:,.0f} m {compass(candidate.search_bearing_deg)} of the search centre.</p>"
        if candidate.search_distance_m is not None else ""
    )
    return (
        f"<article class='pcard' id='pc-{esc(candidate.candidate_id)}' data-rank='{candidate.rank}' style='--rc:{rating_colour}'>"
        f"<header><span class='pid'>{esc(candidate.candidate_id)}</span><span class='prate' style='background:{rating_colour}'>{esc(candidate.rating)} {candidate.score:.0f}</span>"
        f"<span class='small'>rank {candidate.rank} of {total:,}</span></header>"
        f"<div class='pgrid'><section><h4>Where</h4>{position}{elevation}{centre}{area}</section>"
        f"<section><h4>How to come in</h4>{way}{directions}{wind}</section><section>{_pilot_rose(candidate, step_deg)}"
        f"<p class='small'>Rose: where you can come in FROM. Green clear, amber steep, red blocked, grey no data. Blue arrow: the best approach.</p></section></div>"
        f"<p class='plain'><b>In plain words:</b> {esc(plain_summary(candidate, aircraft))}</p>"
        f"<ul class='pcaution'>{''.join(f'<li>{esc(c)}</li>' for c in cautions)}</ul></article>"
    )


def pilot_section(candidates: list[Candidate], aircraft: dict[str, Any], metadata: dict[str, Any], rating_colours: dict[str, str], step_deg: float) -> tuple[str, str]:
    """Return (html of the pilot brief, JSON for the wind helper)."""
    shown = candidates[:PILOT_SITE_LIMIT]
    doctrine = metadata.get("profiles", {}).get("doctrine", {})
    status = "Thresholds are planning values, not approved doctrine." if str(doctrine.get("approval_status", "")).lower() != "approved" else "Thresholds come from an approved profile."
    listed = bool(metadata.get("obstacles"))
    cards = "".join(pilot_card(c, aircraft, step_deg, rating_colours.get(c.rating, "#9e9e9e"), len(candidates), status, listed) for c in shown)
    data = {"step": step_deg, "sites": [{"id": c.candidate_id, "sec": "".join(SECTOR_LETTER.get(s.classification, "I") for s in sorted(c.sectors, key=lambda s: s.azimuth_deg))} for c in shown]}
    name = aircraft.get("display_name", "")
    options = "".join(f"<option value='{n}'{' selected' if n == min(10, len(shown)) else ''}>{n}</option>" for n in sorted({5, 10, 20, PILOT_SITE_LIMIT, len(shown)} & set(range(1, len(shown) + 1)) or {len(shown)}))
    header = (
        "<div id='pilot' hidden><h2>Pilot brief</h2>"
        "<p class='pbanner'><b>For planning only: not for navigation.</b> One card per site, best first. Bearings are TRUE. "
        f"Aircraft class used: {_esc(str(name))}. Thresholds: {_esc(status)} Generated {_esc(str(metadata.get('run_started_utc', '')))} UTC.</p>"
        "<div class='windbar'><b>Wind helper</b> &nbsp; wind FROM <input id='pwd' type='number' min='0' max='360' step='5' placeholder='e.g. 270' aria-label='Wind from, degrees true'>&deg; true"
        " &nbsp; speed <input id='pws' type='number' min='0' step='1' placeholder='kt' aria-label='Wind speed in knots'> kt"
        f" &nbsp; show the best <select id='pn' aria-label='Number of sites'>{options}</select> sites"
        " &nbsp; <button type='button' id='pprint'>Print</button> <button type='button' id='pdl'>Download as a file</button></div>"
    )
    return header + cards + "</div>", _json(data)


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")


EXTRA_CSS = r"""
.viewbar { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 4px; }
.viewbar button, .windbar button { font: inherit; font-size: 14px; padding: 6px 14px; border: 1px solid #0d47a1; background: #fff; color: #0d47a1; border-radius: 16px; cursor: pointer; }
.viewbar button[aria-pressed="true"] { background: #0d47a1; color: #fff; }
.viewbar button:hover, .windbar button:hover, .viewbar button:focus-visible, .windbar button:focus-visible { background: #0d47a1; color: #fff; outline: none; }
.tip { position: relative; display: inline-block; margin-left: 5px; vertical-align: middle; }
.tip i { display: inline-block; width: 16px; height: 16px; line-height: 16px; border-radius: 50%; background: #0d47a1; color: #fff; font: 700 11px/16px Arial, sans-serif; text-align: center; cursor: help; }
.tipbox { display: none; position: absolute; left: -10px; top: 24px; width: 300px; padding: 10px 12px; background: #fff; color: #212121; border: 1px solid #0d47a1; border-radius: 6px; box-shadow: 0 4px 14px rgba(0,0,0,.25); font: 400 12.5px/1.45 "Segoe UI", Arial, sans-serif; text-align: left; z-index: 60; white-space: normal; }
.tip.r .tipbox { left: auto; right: -10px; }
.tip:hover .tipbox, .tip:focus .tipbox, .tip:focus-within .tipbox { display: block; }
.tipbox em { color: #0d47a1; font-style: normal; font-weight: 700; }
#howto { border: 1px solid var(--line); border-radius: 6px; padding: 4px 14px 10px; margin: 8px 0 12px; background: #fbfdfe; }
#howto summary { cursor: pointer; font-weight: 700; padding: 6px 0; }
table.howto th, table.howto td { font-size: 13px; vertical-align: top; }
table.howto tbody th { white-space: nowrap; background: #f5f9fb; }
.good { color: #1a7f37; }
dl.gloss { margin: 0; display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px; font-size: 13px; }
dl.gloss dt { font-weight: 700; } dl.gloss dd { margin: 0; }
.plain { background: #f3f8fb; border-left: 4px solid #0d47a1; padding: 8px 12px; border-radius: 3px; margin: 8px 0; font-size: 13.5px; }
td small, .small small { color: var(--muted); }
body.pilot-mode #planner { display: none; }
#pilot h2 { margin-top: 18px; }
.pbanner { background: #fff3cd; border: 1px solid #f0ad00; padding: 8px 12px; border-radius: 5px; font-size: 13px; }
.windbar { position: sticky; top: 0; z-index: 20; background: #eaf2fb; border: 1px solid #b6cdea; border-radius: 6px; padding: 8px 12px; margin: 10px 0; font-size: 14px; }
.windbar input { width: 78px; font: inherit; padding: 3px 6px; } .windbar select { font: inherit; padding: 3px; }
.pcard { border: 1px solid var(--line); border-left: 10px solid var(--rc, #888); border-radius: 6px; padding: 12px 18px; margin: 16px 0; background: #fff; break-inside: avoid; }
.pcard header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 16px; border-bottom: 1px solid var(--line); padding-bottom: 6px; }
.pid { font-size: 28px; font-weight: 700; } .prate { color: #fff; padding: 2px 12px; border-radius: 12px; font-size: 14px; }
.pgrid { display: grid; grid-template-columns: 1fr 1.2fr 180px; gap: 20px; margin-top: 8px; }
.pgrid h4 { margin: 10px 0 4px; font-size: 13px; color: #0d47a1; } .pgrid p { margin: 4px 0; font-size: 14px; }
.pmgrs { font: 700 21px Consolas, "Courier New", monospace; margin: 4px 0; }
.pbig { font-size: 19px; font-weight: 700; line-height: 1.3; }
.pwarn { color: #b71c1c; font-weight: 700; }
table.pdir { border-collapse: collapse; font-size: 13px; margin: 8px 0; width: 100%; } table.pdir caption { text-align: left; font-weight: 700; padding: 2px 0; }
table.pdir th { text-align: left; width: 38%; background: none; position: static; border-bottom: 1px solid var(--line); } table.pdir td { border-bottom: 1px solid var(--line); }
table.pdir i { display: inline-block; width: 11px; height: 11px; border-radius: 2px; margin-right: 6px; vertical-align: -1px; }
.windres { background: #eaf2fb; border-radius: 4px; padding: 6px 9px; font-size: 13.5px; font-weight: 600; }
.pcaution { font-size: 12.5px; color: #444; margin: 6px 0 0; padding-left: 18px; }
.prose { display: block; margin: 8px auto 0; }
@media (max-width: 900px) { .pgrid { grid-template-columns: 1fr; } }
@media print {
  body.pilot-mode #planner, .viewbar, .windbar, .tip { display: none !important; }
  #pilot[hidden] { display: none; } body.pilot-mode #pilot { display: block; }
  .pcard { page-break-after: always; margin: 0 0 10px; border-width: 1px 1px 1px 8px; }
  body.pilot-mode h1 { font-size: 18px; }
}
"""

PILOT_BASE_CSS = r"""
:root { --ink:#212121; --muted:#616161; --line:#e0e0e0; }
body { font-family: "Segoe UI", Arial, sans-serif; color: var(--ink); margin: 16px auto; max-width: 1150px; padding: 0 14px; }
.small { color: var(--muted); font-size: 12.5px; }
.pilotdoc #pilot { display: block !important; }
"""

PILOT_JS = r"""
(function () {
  var dataEl = document.getElementById('pilotdata'), D = dataEl ? JSON.parse(dataEl.textContent) : {step: 10, sites: []}, by = {};
  D.sites.forEach(function (s) { by[s.id] = s; });
  var names = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
  function pad(v) { return ('000' + Math.round(((v % 360) + 360) % 360)).slice(-3); }
  function comp(b) { return names[Math.round((((b % 360) + 360) % 360) / 22.5) % 16]; }
  function diff(a, b) { var d = Math.abs(a - b) % 360; return d > 180 ? 360 - d : d; }
  function nearest(sec, want, cls) {
    var best = null;
    sec.split('').forEach(function (c, i) { if (c === cls) { var az = i * D.step, d = diff(az, want); if (!best || d < best.d) best = {az: az, d: d}; } });
    return best;
  }
  function update() {
    var w = parseFloat(document.getElementById('pwd').value), sp = parseFloat(document.getElementById('pws').value);
    Array.prototype.forEach.call(document.querySelectorAll('.windres'), function (p) {
      var s = by[p.getAttribute('data-for')]; if (!s) return;
      if (isNaN(w)) { p.textContent = 'Enter the wind above to see the best approach for it.'; return; }
      var want = (w + 180) % 360, clear = nearest(s.sec, want, 'C'), steep = nearest(s.sec, want, 'M'), pick = clear || steep;
      if (!pick) { p.textContent = 'No clear or steep approach direction at this site.'; return; }
      var hdg = (pick.az + 180) % 360, off = diff(hdg, w);
      var t = 'Wind from ' + pad(w) + '\u00b0: come in from ' + pad(pick.az) + '\u00b0 (' + comp(pick.az) + '), land on heading ' + pad(hdg) + '\u00b0 (' + comp(hdg) + '), ' + Math.round(off) + '\u00b0 off the wind';
      if (!isNaN(sp) && sp > 0) t += ' (headwind ' + Math.round(sp * Math.cos(off * Math.PI / 180)) + ' kt, crosswind ' + Math.round(sp * Math.sin(off * Math.PI / 180)) + ' kt)';
      t += '.';
      if (!clear) t += ' Only a STEEP approach is available.';
      else if (steep && steep.d + 30 < clear.d) t += ' A steep approach from ' + pad(steep.az) + '\u00b0 is closer to the wind.';
      if (off > 45) t += ' This is more than 45\u00b0 off the wind.';
      p.textContent = t;
    });
  }
  function showCount() {
    var sel = document.getElementById('pn'); if (!sel) return;
    var n = parseInt(sel.value, 10);
    Array.prototype.forEach.call(document.querySelectorAll('.pcard'), function (c, i) { c.hidden = i >= n; });
  }
  ['pwd', 'pws'].forEach(function (id) { var e = document.getElementById(id); if (e) e.addEventListener('input', update); });
  var pn = document.getElementById('pn'); if (pn) pn.addEventListener('change', showCount);
  var pp = document.getElementById('pprint'); if (pp) pp.addEventListener('click', function () { window.print(); });
  var vp = document.getElementById('vplan'), vq = document.getElementById('vpilot'), vr = document.getElementById('vprint');
  function view(pilot) {
    document.body.classList.toggle('pilot-mode', pilot);
    var pl = document.getElementById('pilot'); if (pl) pl.hidden = !pilot;
    if (vp) vp.setAttribute('aria-pressed', String(!pilot)); if (vq) vq.setAttribute('aria-pressed', String(pilot));
    window.scrollTo(0, 0);
    if (!pilot && window.HLZ) setTimeout(function () { window.HLZ.map.invalidateSize(); }, 50);
  }
  if (vp) vp.addEventListener('click', function () { view(false); });
  if (vq) vq.addEventListener('click', function () { view(true); });
  if (vr) vr.addEventListener('click', function () { window.print(); });
  var dl = document.getElementById('pdl');
  if (dl) dl.addEventListener('click', function () {
    var css = document.getElementById('pilotcss').textContent, code = document.getElementById('pilotjs').textContent;
    var pilot = document.getElementById('pilot').cloneNode(true); pilot.removeAttribute('hidden');
    var doc = '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>HLZ pilot brief</title><style>' + css +
      '</style></head><body class="pilotdoc pilot-mode">' + pilot.outerHTML + '<script id="pilotdata" type="application/json">' + document.getElementById('pilotdata').textContent +
      '<\/script><script id="pilotjs" type="text/plain">' + code + '<\/script><script>' + code + '<\/script></body></html>';
    var a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([doc], {type: 'text/html'})); a.download = 'HLZ_Pilot_Brief.html';
    document.body.appendChild(a); a.click(); setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  });
  update(); showCount();
})();
"""


def viewbar_html() -> str:
    return (
        '<nav class="viewbar" aria-label="Report views"><button type="button" id="vplan" aria-pressed="true">Planner report</button>'
        '<button type="button" id="vpilot" aria-pressed="false">Pilot brief</button><button type="button" id="vprint">Print</button></nav>'
    )


def pilot_scripts(pilot_json: str) -> str:
    css = EXTRA_CSS + PILOT_BASE_CSS
    return (
        f'<style id="pilotcss" type="text/plain" media="not all">{css}</style>\n'
        f'<script id="pilotdata" type="application/json">{pilot_json}</script>\n'
        f'<script id="pilotjs" type="text/plain">{PILOT_JS}</script>\n<script>{PILOT_JS}</script>'
    )


def pilot_document(title: str, section_html: str, pilot_json: str) -> str:
    """The pilot brief as its own stand-alone HTML file."""
    css = EXTRA_CSS + PILOT_BASE_CSS
    body = section_html.replace("<div id='pilot' hidden>", "<div id='pilot'>", 1).replace(" <button type='button' id='pdl'>Download as a file</button>", "")
    return (
        "<!DOCTYPE html>\n<!-- SPDX-License-Identifier: GPL-2.0-or-later -->\n<!-- Copyright (c) 2026 Eui Soo SON -->\n"
        f'<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{_esc(title)}</title>'
        f"<style>{css}</style></head><body class=\"pilotdoc pilot-mode\"><h1>{_esc(title)}</h1>{body}"
        f'<script id="pilotdata" type="application/json">{pilot_json}</script><script>{PILOT_JS}</script></body></html>\n'
    )
