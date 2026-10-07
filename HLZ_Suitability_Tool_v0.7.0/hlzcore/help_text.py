# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Plain-language explanations for every toolbox field (v0.7.0).

One source for two uses:

* ``scripts/build_tool_metadata.py`` turns ``FIELD_HELP`` into the ArcGIS Pro
  metadata file (``HLZ_Suitability.AnalyzeHLZ.pyt.xml``) that drives the
  hover tooltip (the (i) icon) of each parameter;
* the toolbox dialog shows ``guidance(...)`` as context-aware messages under
  each field while "Explain each field" is ticked.

Each entry: ``what`` (what the field is), ``requires`` (what to enter),
``effect`` (what it changes in the results) and optional ``example``.
"""

from __future__ import annotations

from typing import Any

FIELD_HELP: dict[str, dict[str, str]] = {
    "data_root": {
        "title": "HLZ data folder",
        "what": (
            "A folder made by '0. Set Up and Audit Data Folder' (HLZ_DATA), with the DTM, DSM, water and other "
            "inputs in standard sub-folders."
        ),
        "requires": (
            "Optional. The data folder itself (the one holding hlz_data_root.json). Exactly one raster in "
            "01_Elevation\\DTM (and in DSM) is needed for them to be chosen automatically."
        ),
        "effect": (
            "EMPTY fields are filled from the folder: DTM, DSM, exclusion areas (02_Hydrology + 03_Exclusions), obstacles (10_Obstacles), the "
            "AOI (if 04_AOI holds exactly one polygon layer) and the output folder (08_Runs). Fields you already "
            "filled are never changed. Search centres are not filled in."
        ),
        "example": "C:\\GIS\\HLZ_DATA",
    },
    "data_parent": {
        "title": "Data folder location",
        "what": "The folder in which the HLZ data folder is (or will be) created.",
        "requires": (
            "An existing local folder with a short path, such as C:\\GIS. Not OneDrive, Dropbox or another synced "
            "folder. Choosing an existing data folder itself also works."
        ),
        "effect": "The data folder is <this folder>\\<Data folder name>.",
        "example": "C:\\GIS",
    },
    "data_name": {
        "title": "Data folder name",
        "what": "Name of the data folder.",
        "requires": "Letters, digits, '_' or '-'. Default HLZ_DATA.",
        "effect": "Ignored when the location chosen above is already a data folder.",
    },
    "create_layout": {
        "title": "Create missing folders",
        "what": "Creates the standard sub-folders, a README in each and the hlz_data_root.json marker.",
        "requires": "Nothing.",
        "effect": "Only ADDS what is missing. Nothing is ever moved, renamed, overwritten or deleted. Untick to audit only.",
    },
    "audit_aircraft": {
        "title": "Aircraft for the margin check",
        "what": "The aircraft whose approach range sets how far the DTM must extend beyond the AOI.",
        "requires": "Any aircraft in the list (generic medium by default).",
        "effect": "Only changes the AOI margin check (approach range + touchdown point + ring).",
    },
    "open_audit_report": {
        "title": "Open the audit report",
        "what": "Opens 09_Audit\\HLZ_Data_Audit_latest.html in the web browser when finished.",
        "requires": "Nothing.",
        "effect": "The report is always written; this only opens it.",
    },
    "in_dtm": {
        "title": "Bare-earth elevation (DTM)",
        "what": "Digital Terrain Model: ground elevation with trees and buildings removed.",
        "requires": (
            "A single-band elevation raster in a PROJECTED coordinate system with metre units "
            "(e.g. NAD83(CSRS) / UTM zone 18N for Ottawa, zone 17N for Toronto). Square cells; "
            "1 m or finer is recommended. Any size: large areas are processed in tiles."
        ),
        "effect": (
            "Used for slope, surface roughness and terrain masking of approaches. A coarse DTM "
            "(> 2.5 m) lowers the confidence of every result."
        ),
        "example": "Ottawa_DTM_1m.tif (NRCan HRDEM 'DTM' product)",
    },
    "in_dsm": {
        "title": "Surface model (DSM)",
        "what": (
            "Digital Surface Model: elevation of the top of everything - trees, buildings, "
            "towers. DSM minus DTM = obstacle height."
        ),
        "requires": (
            "Optional but strongly recommended. Same area as the DTM; a different cell size or "
            "coordinate system is resampled onto the DTM grid automatically (slower on big areas)."
        ),
        "effect": (
            "Without a DSM, trees and buildings at the LZ and along the approach are NOT detected, "
            "so every rating is capped at 'Marginal'."
        ),
        "example": "Ottawa_DSM_1m.tif (NRCan HRDEM 'DSM' product)",
    },
    "in_aoi": {
        "title": "Area of interest (polygon)",
        "what": "Where landing-zone CENTRES may be placed.",
        "requires": (
            "Optional polygon layer (any coordinate system; selected features only if a "
            "selection exists). No size limit - a city boundary works."
        ),
        "effect": (
            "Limits the search and the run time. Terrain outside the polygon is still read for "
            "approach checks (up to the aircraft approach range, ~1 km)."
        ),
        "example": "City_of_Ottawa_boundary",
    },
    "use_map_view": {
        "title": "If no AOI is given, use the current map view extent",
        "what": "Uses the rectangle currently visible in the active map as the area of interest.",
        "requires": "Only applies when the AOI field is empty and a map view is active.",
        "effect": (
            "Zoom to an area and run - no polygon needed. Untick it to analyse the WHOLE DTM "
            "when no AOI is drawn."
        ),
    },
    "in_centre": {
        "title": "Search around a point: centre point layer",
        "what": "One or more points; landing zones are searched for only within the search radius of them.",
        "requires": "Optional. A POINT layer (digitise one with Create Features, or use an existing layer). Needs a search radius. A selection is respected.",
        "effect": (
            "Only the circle(s) around the points are analysed, so a small search finishes in seconds even on a huge DTM. "
            "Every candidate reports its distance and bearing from the nearest centre. If an AOI is also given, only the overlap is searched."
        ),
    },
    "centre_text": {
        "title": "Search around a point: centre typed as MGRS or latitude, longitude",
        "what": "A single search centre typed as text, added to any points from the point layer.",
        "requires": "Optional. An MGRS grid reference or a latitude, longitude, on WGS84. Needs a search radius.",
        "effect": "Same as the point layer, without having to create a layer.",
        "example": "18T VR 45906 30941   or   45.4236, -75.7009   or   45.4236 N 75.7009 W",
    },
    "search_radius_m": {
        "title": "Search around a point: radius (m)",
        "what": "How far from each centre point landing-zone centres may be placed.",
        "requires": "Required when a centre is given; 1 to 5,000,000 m. Ignored without a centre.",
        "effect": (
            "The search circle is limited to where you have data. Terrain and obstacles are still read beyond the circle "
            "(the approach range plus 45 m for the medium class) to check approaches."
        ),
        "example": "2000 (a 2 km search around the point)",
    },
    "in_exclusion": {
        "title": "Exclusion areas (polygons, lines or points)",
        "what": (
            "Polygons, lines or points where a landing zone must not be placed: lakes, ponds, wide rivers, reservoirs, "
            "wetlands (polygons), rivers, streams, canals and shorelines (lines), single known hazards such as a "
            "crane, a pole or a wellhead (points), and anything else that looks flat and smooth in elevation data "
            "but is not firm ground."
        ),
        "requires": (
            "Optional, strongly recommended. One or more POLYGON, LINE or POINT layers, in any coordinate system and "
            "of any size (a province-wide water layer works; only the part near each tile is read). Lines are widened "
            "by the line width field below, and points are given the point radius below, because neither has a size "
            "of its own."
        ),
        "effect": (
            "A touchdown point can never overlap these areas. Candidates within the cleared ring of one are "
            "annotated and show their distance to it. Without this layer a lake is rated like a perfect flat field. "
            "A closed lake outline drawn as a LINE excludes only the strip along the shore, not the lake: use the "
            "lake POLYGON for that."
        ),
        "example": "Ontario Hydro Network - Waterbody; NRCan National Hydrographic Network (NHN) waterbodies; USGS NHD / 3DHP waterbodies; OpenStreetMap water",
    },
    "line_width_m": {
        "title": "Width given to exclusion lines (m)",
        "what": "Total width given to every LINE in the exclusion layers (rivers, streams, canals, shorelines).",
        "requires": "A number from 0 to 500; default 10. Half of it is applied on each side of the line.",
        "effect": (
            "A touchdown point can never touch a line widened by this much. The cleared-ring distance is added on "
            "top of it, as for polygons. Give wide rivers as polygons, or set the width to about the river's width. "
            "Ignored when no line layer is used."
        ),
        "example": "10 for a stream, 40 for a small river",
    },
    "point_radius_m": {
        "title": "Radius given to exclusion points (m)",
        "what": "Radius of the circle excluded around every POINT in the exclusion layers (a crane, a pole, a wellhead, a known hazard).",
        "requires": "A number from 0 to 500; default 10. 0 excludes only the cell that holds the point.",
        "effect": (
            "A touchdown point can never touch a circle of this radius. The cleared-ring distance is added on top of "
            "it, as for polygons and lines. Large hazards should come as polygons. Ignored when no point layer is used."
        ),
        "example": "10 for a pole, 25 for a crane with a swing radius",
    },
    "build_overview": {
        "title": "Create a terrain overview grid",
        "what": (
            "An extra map layer of coarse square cells that shows, for each cell, how steep and rough the ground is, "
            "how much of it passes the ground test, and how many ranked candidates it holds."
        ),
        "requires": "Optional. Tick it for a large area (a city) to see at a glance where to look; leave it off for a small area.",
        "effect": (
            "Written as HLZ_Overview_Grid in the results geodatabase and added to the map, coloured by the share of each "
            "cell that passes the ground test. It is built from the same slope, roughness and ground-test results the "
            "candidates come from, so it always agrees with them. It costs almost no extra time or memory."
        ),
    },
    "overview_cell_m": {
        "title": "Overview grid cell size (m)",
        "what": "The side length of one overview cell.",
        "requires": (
            "A number of metres from 20 to 20,000; default 500. A cell always holds at least 16 analysis cells, so on a "
            "10 m DTM the smallest cell is 160 m; the run log says when a cell was enlarged."
        ),
        "effect": "Smaller cells show more detail and make a bigger layer. 250 to 1,000 m suits a city.",
        "example": "500",
    },
    "in_obstacle_layers": {
        "title": "Obstacles - layers (towers, masts, wires, buildings)",
        "what": (
            "GIS layers of tall things that a lidar surface model often misses: communication towers, antennas, "
            "wind turbines, power lines, chimneys and buildings."
        ),
        "requires": (
            "Optional, strongly recommended. Point, line or polygon layers (shapefile, GeoPackage or geodatabase "
            "feature class) with a height ABOVE GROUND in a field such as height, agl or hgt. Any coordinate "
            "system. Features without a height get the default height below and are marked ASSUMED in the report."
        ),
        "effect": (
            "Each obstacle is added to the surface at its ground height plus its height. A landing zone cannot be "
            "placed on or beside one that is taller than the obstacle limits, and every approach path is checked "
            "against it exactly, even a mast too thin for the normal check to hit. Without this, a tower in the "
            "glide path can be rated clear."
        ),
        "example": "A shapefile of towers with an AGL_M field; an OpenStreetMap power-line layer with a height field",
    },
    "in_obstacle_files": {
        "title": "Obstacles - FAA DOF, CSV or GeoJSON files",
        "what": "Obstacle lists in common file formats, read directly.",
        "requires": (
            "Optional. FAA Digital Obstacle File (DOF.DAT, United States only, heights in feet above ground), a CSV "
            "with latitude, longitude and height columns (WGS84), or GeoJSON (WGS84, including OpenStreetMap exports "
            "with man_made / power / building tags). Only obstacles near the DTM are read."
        ),
        "effect": "Same as the obstacle layers. Several files can be combined.",
        "example": "C:\\GIS\\HLZ_DATA\\10_Obstacles\\DOF.DAT",
    },
    "obstacle_height_field": {
        "title": "Obstacle height field",
        "what": "The name of the field, column or property that holds the obstacle height above ground.",
        "requires": (
            "Optional. Leave it empty and the tool looks for height, agl, hgt, height_m, height_ft, obst_height "
            "and similar names. Name it when yours is called something else."
        ),
        "effect": "Used for every obstacle layer and file. A field name ending in _ft is read as feet.",
        "example": "TOWER_HGT",
    },
    "obstacle_height_units": {
        "title": "Obstacle height units",
        "what": "The unit of the obstacle heights.",
        "requires": "Meters or Feet. A unit written in the value itself (for example '100 ft') wins. FAA DOF is always feet and is converted for you.",
        "effect": "Wrong units change every obstacle's height by a factor of 3.28.",
    },
    "obstacle_default_height_m": {
        "title": "Height used for obstacles that have none",
        "what": "The height assumed for an obstacle that has no usable height value.",
        "requires": "A number of metres from 1 to 500; default 30.",
        "effect": "Those obstacles are marked ASSUMED in the report. A tall tower with a missing height can be taller than 30 m, so check them.",
        "example": "30",
    },
    "obstacle_buffer_m": {
        "title": "Horizontal buffer around each obstacle (m)",
        "what": "Extra width added around every obstacle to allow for position error and guy wires.",
        "requires": "A number of metres from 0 to 200; default 10.",
        "effect": "Larger values keep landing zones further from obstacles and make approaches more cautious.",
        "example": "10",
    },
    "report_basemap": {
        "title": "Basemap in the HTML report",
        "what": "Whether the interactive map in HLZ_Report.html loads online map tiles under the candidates.",
        "requires": "Online imagery and topographic map (needs internet when the report is opened) or None.",
        "effect": (
            "Online: the viewer's browser requests map tiles from third-party servers (Esri, OpenStreetMap), "
            "which reveals the area being viewed. None: the report makes no network requests; candidates are "
            "drawn on a plain background. Choose None for sensitive work."
        ),
    },
    "z_units": {
        "title": "Elevation (Z) units",
        "what": "The unit of the elevation VALUES stored in the DTM/DSM cells.",
        "requires": (
            "Meters for Canadian (HRDEM/CDEM) and most international data; Feet for many US "
            "state/county lidar products. Horizontal units must be metres regardless."
        ),
        "effect": (
            "Wrong units scale every slope and obstacle height by 3.28 - e.g. a 7 deg slope would "
            "be read as 22 deg."
        ),
    },
    "aircraft": {
        "title": "Aircraft profile",
        "what": "The physical limits of the helicopter class the LZ must accept.",
        "requires": (
            "Pick the helicopter from the list (generic light / medium / heavy, or a named Canadian, US or NATO "
            "type; each entry shows its landing-point and ring size), or 'Custom profile file...'. Shipped "
            "values are PLANNING DEFAULTS, not operator data."
        ),
        "effect": (
            "Sets the touchdown point (TDP) diameter, cleared ring, maximum slope (any heading / "
            "upslope only), roughness and obstacle limits, approach range and the 3/7 deg "
            "approach-angle thresholds."
        ),
    },
    "aircraft_file": {
        "title": "Custom aircraft profile (.json)",
        "what": "Your own aircraft limits (copy profiles/aircraft/generic_medium.json and edit).",
        "requires": "A .json file; check it first with '2. Validate Profiles'.",
        "effect": "Replaces the shipped class limits for this run.",
    },
    "oat_c": {
        "title": "Outside air temperature (C)",
        "what": "Expected air temperature at the LZ at the time of the mission.",
        "requires": "Optional. Degrees Celsius, -70 to +60.",
        "effect": (
            "With the altimeter setting, gives DENSITY ALTITUDE - how 'high' the aircraft feels. "
            "Hot air is thin: an Ottawa LZ at 70 m elevation behaves like ~280 ft at 15 C but "
            "~2,100 ft at 30 C. Sites above the aircraft's hover ceiling are rated Unsuitable "
            "(only if the profile has ceilings)."
        ),
        "example": "28 (a July afternoon in Ottawa)",
    },
    "altimeter_inhg": {
        "title": "Altimeter setting (inHg)",
        "what": "Local sea-level pressure (QNH) as set on the aircraft altimeter.",
        "requires": (
            "Inches of mercury, 25.0-32.5. 29.92 = standard atmosphere. From hPa: divide by 33.864 "
            "(1013 hPa = 29.92 inHg)."
        ),
        "effect": "Low pressure raises density altitude (~1,000 ft per 1 inHg below 29.92).",
        "example": "29.80 (from the METAR 'A2980')",
    },
    "hover_mode": {
        "title": "Hover performance mode (OGE / IGE)",
        "what": (
            "Which hover-ceiling limit of the aircraft profile is compared with density altitude. "
            "OGE = Out of Ground Effect: hovering more than about one rotor diameter above the "
            "ground - no air cushion, MORE power needed. IGE = In Ground Effect: hovering close to "
            "the ground, where the downwash cushion reduces the power required."
        ),
        "requires": (
            "OGE (default, conservative) for confined, obstructed, sloping or vegetated LZs and "
            "any vertical approach/departure. IGE only when a low hover over firm, flat ground "
            "is assured."
        ),
        "effect": (
            "Only used when an OAT is entered AND the aircraft profile defines hover ceilings; "
            "otherwise it has no effect (shipped generic profiles define none)."
        ),
    },
    "out_folder": {
        "title": "Output folder",
        "what": "Parent folder for results.",
        "requires": "An existing folder with free space (large areas: ~1-3 GB per 1,000 km2 at 1 m).",
        "effect": "A sub-folder named after the run is created inside it.",
    },
    "run_name": {
        "title": "Run name",
        "what": "Name of the results sub-folder.",
        "requires": "Letters, digits, '_' or '-' (other characters are replaced).",
        "effect": (
            "Re-using the name of an UNFINISHED run resumes it from the last finished tile. A "
            "finished run is never overwritten - a numbered copy is made."
        ),
    },
    "add_to_map": {
        "title": "Add styled results to the current map",
        "what": "Adds the result layers with colours, labels and approach arrows.",
        "requires": "An open map view.",
        "effect": "Untick for batch/scripted runs.",
    },
    "open_report": {
        "title": "Open the HTML report when finished",
        "what": "Opens HLZ_Report.html in the default web browser.",
        "requires": "Nothing.",
        "effect": "The report is always written; this only opens it.",
    },
    "mission_file": {
        "title": "Mission profile (.json)",
        "what": "Scoring weights and processing settings.",
        "requires": "screening.json (thorough) or quick_look.json (faster, fewer candidates).",
        "effect": (
            "Controls score weights, approach sampling, candidate numbers and max_input_cells = "
            "cells per TILE (memory use ~140 bytes/cell; 16 M ~ 2.3 GB). Lower it on PCs with "
            "little RAM; it never limits the total area."
        ),
    },
    "doctrine_file": {
        "title": "Doctrine / threshold profile (.json)",
        "what": "Score thresholds for each rating and their approval status.",
        "requires": "generic_planning.json unless your authority has approved a profile.",
        "effect": (
            "Ratings: Highly suitable >= 80, Suitable >= 60, Marginal >= 40. Unapproved profiles "
            "label every result 'Planning only'."
        ),
    },
    "max_candidates": {
        "title": "Maximum number of candidates",
        "what": "Total number of LZ candidates kept (best first).",
        "requires": "Optional, 1-100,000. Blank = mission profile (1,000 for screening).",
        "effect": (
            "City-wide runs can find tens of thousands; the HTML table shows the best 500, the "
            "CSV and geodatabase hold all."
        ),
    },
    "diagnostic_rasters": {
        "title": "Slope / roughness / obstacle rasters",
        "what": "Extra full-resolution rasters explaining WHY ground passed or failed.",
        "requires": "AUTO (default), ALWAYS or NEVER.",
        "effect": (
            "AUTO writes them only for areas up to 100 M cells (they cost ~12 bytes/cell of disk "
            "and time). Ground_Class and all candidate outputs are always written."
        ),
    },
    "show_guidance": {
        "title": "Explain each field in the dialog",
        "what": "Shows these explanations under each field, adapted to your current inputs.",
        "requires": "Nothing.",
        "effect": "Untick once you know the tool; real problems are always reported.",
    },
}


CLOUD_FOLDER_TAGS = ("onedrive", "dropbox", "google drive", "icloud", "sharepoint", "box sync")
LONG_PATH_LIMIT = 120


def path_warning(path: str | None) -> str | None:
    """Warn about folders that are slow or fragile for large runs, else None.

    * Cloud-synced folders (OneDrive, Dropbox, ...): the sync client re-uploads and locks
      every file, which slows runs and can break the file geodatabase.
    * Very long paths: Windows stops at 260 characters, and tile files and geodatabase
      names are added below the run folder.
    """
    if not path:
        return None
    lowered = path.lower()
    if any(tag in lowered for tag in CLOUD_FOLDER_TAGS):
        return (
            f"'{path}' is inside a cloud-synced folder (OneDrive, Dropbox, ...). The sync client "
            "locks and re-uploads every file, which slows large runs and can corrupt a file "
            "geodatabase. Use a local folder such as C:\\GIS\\HLZ_runs."
        )
    if len(path) > LONG_PATH_LIMIT:
        return (
            f"This folder path is {len(path)} characters long. Windows stops at 260 including the "
            "file names created below it, so tile files may fail to save. Use a shorter path such "
            "as C:\\GIS\\HLZ_runs."
        )
    return None


def tooltip_text(name: str) -> str:
    """Full help text for the metadata tooltip."""
    entry = FIELD_HELP.get(name)
    if not entry:
        return ""
    parts = [entry["what"], "Requires: " + entry["requires"], "Effect: " + entry["effect"]]
    if entry.get("example"):
        parts.append("Example: " + entry["example"])
    return "\n\n".join(parts)


def guidance(name: str, context: dict[str, Any]) -> str | None:
    """Context-aware one-paragraph guidance for the dialog (None = nothing useful to say).

    ``context`` keys used: dsm_given, aoi_given, use_view, oat_given, has_ceilings,
    altimeter, z_units, profile_custom, cell_size, area_km2, tiles, diagnostics.
    """
    entry = FIELD_HELP.get(name)
    if entry is None:
        return None
    c = context

    if name == "hover_mode":
        if not c.get("oat_given"):
            state = "NOT USED in this run: no outside air temperature entered (Weather section)."
        elif not c.get("has_ceilings"):
            state = (
                "NOT USED in this run: the aircraft profile has no hover ceilings (density "
                "altitude is still reported). To use it, add hover_ceiling_oge_ft / "
                "hover_ceiling_ige_ft from the aircraft's performance charts to a custom profile."
            )
        else:
            state = "USED in this run: candidates above the selected hover ceiling become Unsuitable."
        return (
            "OGE = Out of Ground Effect (hover > ~1 rotor diameter up, more power; safe default). "
            "IGE = In Ground Effect (low hover over firm flat ground, less power). " + state
        )
    if name == "oat_c":
        if not c.get("oat_given"):
            return (
                "Optional. Enter the expected temperature to compute density altitude (hot, high "
                "or low-pressure days reduce helicopter lift). Leave blank to skip."
            )
        return "Density altitude will be reported for every candidate. " + (
            "Sites above the hover ceiling will be rejected."
            if c.get("has_ceilings")
            else "The profile has no hover ceilings, so it is reported only, not used to reject sites."
        )
    if name == "altimeter_inhg":
        if not c.get("oat_given"):
            return "Only used with an OAT. Inches of mercury (1013 hPa = 29.92 inHg)."
        if abs(float(c.get("altimeter") or 29.92) - 29.92) < 1.0e-6:
            return "Standard pressure (29.92 inHg) assumed. Enter the local QNH (e.g. METAR 'A2980' = 29.80) for accuracy."
        return None
    if name == "in_dsm" and not c.get("dsm_given"):
        return (
            "No DSM: trees, buildings and towers will NOT be detected; all ratings are capped at "
            "Marginal. Add the DSM that matches the DTM (e.g. HRDEM DSM)."
        )
    if name == "in_aoi":
        if c.get("aoi_given"):
            return (
                "LZ centres are placed only inside this polygon; terrain up to the approach range "
                "around it is still read for approach checks."
            )
        if c.get("use_view"):
            return (
                "Empty: the visible extent of the active map view is used as the area; with no map "
                "view open, the WHOLE DTM is analysed (no size limit; large areas take longer)."
            )
        return "Empty: the WHOLE DTM will be analysed (no size limit; large areas take longer)."
    if name in ("in_centre", "centre_text", "search_radius_m"):
        if c.get("centre_given") and not c.get("radius_given"):
            return "A centre is set: enter the search radius (m) below it."
        if c.get("centre_given"):
            if c.get("aoi_given"):
                return "Only the overlap of the AOI and the search circle is searched. Candidates report their distance and bearing from the centre."
            return "Only the circle around the centre is searched, so it is fast even on a very large DTM. Candidates report their distance and bearing from the centre."
        if name == "in_centre":
            return (
                "Optional. To look for landing zones around a place instead of over a whole area, give a centre "
                "(a point layer, or type an MGRS or latitude, longitude) and a radius."
            )
        return None
    if name == "line_width_m":
        if c.get("lines_given"):
            return (
                "Applies to the line layers among the exclusion areas: every line gets this total width (half each side). "
                "Set it to about the width of your rivers; give wide rivers as polygons."
            )
        return None
    if name == "point_radius_m":
        if c.get("points_given"):
            return (
                "Applies to the point layers among the exclusion areas: each point excludes a circle of this radius, "
                "plus the cleared ring. Use polygons for large hazards."
            )
        return None
    if name == "build_overview":
        return (
            "Tick to add a coarse grid layer showing where the ground passes, for a quick look over a large area. "
            "It adds almost no run time."
        )
    if name == "overview_cell_m":
        if c.get("overview_on"):
            return "Cell side in metres (at least 16 analysis cells). 250 to 1,000 m suits a city."
        return None
    if name == "in_exclusion":
        if c.get("exclusion_given"):
            return (
                "A touchdown point will not touch these polygons. Water layers can be old: a pond or reservoir "
                "made after the survey is not excluded, so still check candidates on imagery."
            )
        return (
            "None given: lakes, ponds and wetlands are NOT excluded and can be rated as perfect landing zones. "
            "Add a water polygon layer (for example Ontario Hydro Network - Waterbody, NRCan NHN, USGS NHD/3DHP "
            "or OpenStreetMap water)."
        )
    if name in ("in_obstacle_layers", "in_obstacle_files"):
        if c.get("obstacles_given"):
            return (
                "Heights are above ground. An obstacle missing from the list is not seen, so this reduces the risk "
                "but does not remove it; still check from the air."
            )
        if name == "in_obstacle_files":
            return (
                "No obstacle list given: towers, masts and wires that the lidar surface misses are NOT considered, "
                "so a glide path can look clear when a tower stands in it. Add an FAA DOF file (US), an "
                "OpenStreetMap GeoJSON, a CSV or an obstacle layer."
            )
        return None
    if name in ("obstacle_height_field", "obstacle_height_units", "obstacle_default_height_m", "obstacle_buffer_m"):
        if not c.get("obstacles_given"):
            return None
        if name == "obstacle_default_height_m":
            return f"Obstacles without a height are assumed to be {c.get('obstacle_default_m') or 30:g} m tall and are marked ASSUMED in the report."
        return None
    if name == "report_basemap":
        if c.get("basemap") == "OFFLINE":
            return "No map tiles: the report makes no network requests. Candidates are drawn on a plain background."
        return (
            "Online map tiles are requested from third-party servers when the report is opened, which reveals "
            "the area viewed. Choose None for sensitive work."
        )
    if name == "z_units":
        return (
            "Unit of the elevation VALUES (not the map units). Canadian HRDEM/CDEM = Meters; many "
            "US lidar = Feet. Wrong units scale slopes and obstacle heights by 3.28."
        )
    if name == "aircraft":
        if c.get("profile_custom"):
            return "Custom profile selected: choose the .json file below."
        sizes = c.get("aircraft_sizes")
        if sizes:
            name_, tdp, ring = sizes
            return (
                f"{name_}: landing point {tdp:g} m across plus a {ring:g} m cleared ring, so the area "
                f"checked around each site is {tdp / 2.0 + ring:g} m in radius. A larger helicopter "
                "needs a bigger area; a smaller one finds more sites. Planning defaults, not operator data."
            )
        return (
            "Generic planning class: sets the LZ size, slope, roughness and obstacle limits and the "
            "approach range. Replace with operator data for operational use."
        )
    if name == "mission_file":
        return (
            "screening = thorough; quick_look = faster, fewer candidates. max_input_cells sets the "
            "tile size (memory), never the total area."
        )
    if name == "doctrine_file":
        return None  # the approval warning is already shown
    if name == "max_candidates":
        return "Blank = mission profile value. City-wide runs: raise to keep more than the best 1,000."
    if name == "diagnostic_rasters":
        area = c.get("area_km2")
        if c.get("diagnostics") == "ALWAYS" and area and area > 100:
            return (
                f"ALWAYS on {area:,.0f} km2 writes about {area * 12 / 1000:,.1f} GB of extra rasters "
                "and adds run time. AUTO skips them above 100 km2 at 1 m."
            )
        return "AUTO writes slope/roughness/obstacle rasters only for areas up to 100 M cells."
    if name == "data_root":
        summary = c.get("data_summary")
        if summary:
            return summary
        return (
            "Optional, and the easiest way to start: choose your HLZ data folder and the DTM, DSM, water/exclusion "
            "layers, AOI and output folder are filled in. Create one with '0. Set Up and Audit Data Folder'."
        )
    if name == "data_parent":
        return "A local folder with a short path, e.g. C:\\GIS. The data folder is created inside it."
    if name == "create_layout":
        return "Only adds missing folders and README files; nothing is moved or deleted."
    if name == "run_name":
        return "Re-use the name of an unfinished run to resume it; finished runs are never overwritten."
    if name == "out_folder":
        return (
            "Needs free disk space: about 1-3 GB per 1,000 km2 at 1 m (more with ALWAYS diagnostic "
            "rasters). Choose a local fast drive such as C:\\GIS\\HLZ_runs, not a cloud-synced folder."
        )
    return None
