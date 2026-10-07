<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- Copyright (c) 2026 Eui Soo SON -->

# HLZ Suitability Tool v0.7.0

An ArcGIS Pro Python toolbox (`.pyt`) that screens high-resolution elevation data for
helicopter landing-zone (HLZ) candidates. It checks slope, surface roughness, obstacles in the
touchdown point (TDP) and cleared ring, approach/departure sectors, and optional density
altitude. It then ranks the candidates and presents them as coloured map layers, an HTML report
and a CSV table, with MGRS grid references.

> **Planning aid only.** It does not certify a landing zone. Every site must be confirmed by
> ground or aerial reconnaissance and with the operator's aircraft performance data. The
> shipped profiles are planning defaults, not approved doctrine.

## What changed in v0.7.0

**A terrain overview grid for large areas, and points as exclusion areas.** Both ideas were taken from a simpler
slope-and-ruggedness tool (SlopeRuggednessTool-Santos) and rebuilt on this tool's own results, so they always agree with
the candidates.

* **Terrain overview grid (optional).** Tick **Also create a terrain overview grid** in **1. Analyze HLZ Suitability**
  and set **Overview grid cell size** (default 500 m; 250 to 1,000 m suits a city). The result is the layer
  `HLZ_Overview_Grid`: one square per cell with
  * `Slope_Mean`, `Slope_Max` (plane-fit slope), `Rough_Mean`, `Elev_Min`, `Elev_Max`, `Elev_Range`;
  * `Pct_Pass` (share of the cell that passes the ground test), split into `Pct_Any` (any landing heading) and
    `Pct_Upslope` (upslope landing only);
  * `Terr_Class` (Flat up to half the aircraft's landing-slope limit, Moderate up to the limit, Steep above it) and
    `Pass_Class` (High from 25 %, Medium from 5 %, Low above 0, None);
  * `Cand_Count` and `Best_Rating`: how many ranked candidates sit in the cell, and the best rating among them;
  * `Cells_Valid` and `Pct_Cover`: how much of the cell holds analysed data (edge cells of an AOI are partial).

  The map layer is coloured by `Pass_Class` and sits under the other results. It is built tile by tile from the same
  slope, roughness and ground-test results as the candidates, stored in the tile checkpoint (an interrupted run resumes
  without rework) and costs almost no extra time or memory. A cell is never smaller than 16 analysis cells; the run log
  says when a requested size was enlarged. `run_metadata.json` has an `overview` block with the class counts and limits.
  Command line: `--overview-cell 500` writes `HLZ_Overview_Grid.geojson`.
* **Points as exclusion areas.** **Exclusion areas** (and `03_Exclusions` in the data folder) now also accept POINT
  layers: a crane, a pole, a wellhead, any single known hazard. A point has no size, so a new field,
  **Radius given to exclusion POINTS (m)** (default 10, 0 to 500), gives each point a circle; the cleared-ring distance is
  added on top, exactly as for polygons and lines. Polygons, lines and points can be mixed. In `02_Hydrology` points are
  still not used (points make no sense as water). The audit reports point layers as INFO. Command line: GeoJSON `Point`
  and `MultiPoint` in `--exclude`, and `--point-radius`.
* **Fixed.** The data-folder audit reported a layer holding a single point as "does not overlap the DTM" even when it lay
  inside, because a single point has a zero-size extent. It now counts as overlapping.
* **Not changed.** The analysis, scoring, ratings and every earlier output are identical to v0.6.9 when the new options
  are left off.
* Toolbox: three new inputs (positions 32-34) before the two derived outputs (now 35-36). Tests: 99 unit tests
  (17 new), 163 command-line checks (24 new), ArcPy smoke scenario K (11 scenarios in all).
* **Not verified here:** everything that needs ArcGIS Pro (the new fields, the point-layer reading, the overview layer and
  its map styling, ArcPy smoke scenario K). Run `RUN_ARCPY_SMOKE_TEST.bat` and return the log. The User Manual and the Data
  Requirements document (.docx) were not rewritten for v0.7.0; this README, the CHANGELOG, the Quick Start and the
  in-dialog help are current.

## What changed in v0.6.9

**Towers, antennas, masts and wires are now taken into account.** A lidar surface model often misses thin structures,
so a glide slope could look clear with a communication tower standing in it. You can now list obstacles and the tool
checks every approach against them.

* **Where to give them.** Two new fields in **1. Analyze HLZ Suitability**: **Obstacles - layers** (points, lines or
  polygons in a shapefile, GeoPackage or geodatabase, with a height field) and **Obstacles - FAA DOF, CSV or GeoJSON
  files**. The data folder has a new `10_Obstacles` folder that fills them automatically.
* **Formats.** FAA Digital Obstacle File (`DOF.DAT`, United States, feet above ground); CSV with latitude, longitude and
  height columns (WGS84); GeoJSON including OpenStreetMap exports (`man_made=mast`, `tower`, `power=line`, `building`
  with `height` or `building:levels`); GIS layers in any coordinate system. The height field is found automatically
  (height, agl, hgt, height_m, height_ft, ...) or named in **Obstacle height field**. Feet are recognised from the field
  name (`_ft`), a unit in the value (`100 ft`) or **Obstacle height units**.
* **What they do.** Each obstacle is burned into the surface at ground height + its height, so slope and obstacle
  checks see it; a landing zone is never placed on or beside one taller than the limits; and every approach sector gets
  an exact test against each listed obstacle, so even a mast thinner than the ray spacing is found. Without a surface
  model the approaches still see the listed obstacles.
* **Missing heights.** An obstacle with no height gets **Height used for obstacles that have none** (default 30 m) and
  is marked ASSUMED in the report and the metadata. **Horizontal buffer** (default 10 m) widens every obstacle.
* **Outputs.** New CSV and geodatabase columns `Nearest_obstacle_m` / `Nearest_obstacle` (`Obst_Dist_m`, `Obst_Name`);
  purple obstacle dots on the report map; the report, pilot brief and plain-words sentence name a listed obstacle that
  limits an approach or is close to the pad; the limitations text says what was and was not checked.
* **Command line.** `--obstacles` (DOF, CSV or GeoJSON files), `--obstacle-height-field`, `--obstacle-units`,
  `--obstacle-default-height`, `--obstacle-buffer`; `--data-root` picks up `10_Obstacles` (GeoJSON, CSV and DOF only).
* **Limits, stated plainly.** An obstacle that is not in your list is not seen. FAA DOF covers the United States and its
  territories only; elsewhere use national aeronautical data, OpenStreetMap or your own survey. DOF parsing was written from
  the FAA format description and tested on a synthetic file: check the first run against a real `DOF.DAT`.
* Toolbox: six new inputs (positions 26-31) before the two derived outputs. Tests: 82 unit tests (8 new), 139 command-line
  checks, ArcPy smoke scenario J.

## What changed in v0.6.8

The **Aircraft profile** drop-down lists each helicopter with its landing-point and ring size (for example CH-47F: 80 m
+ 30 m ring; UH-72A Lakota: 25 m + 10 m ring). Pick the type and the analysis uses that size; the guidance under the
field shows the resulting radius. Unit tests: 74.

## What changed in v0.6.7

* **Water LINES can be exclusion areas.** **Exclusion areas** (and `02_Hydrology` / `03_Exclusions` in the data folder) now
  accept line layers - rivers, streams, canals, shorelines, and also power lines or roads - as well as polygons. A line has
  no width, so a new field, **Width given to exclusion LINES (m)** (default 10, total width, half each side), widens
  every line; the cleared-ring distance is added on top, as for polygons. Mixed polygon and line layers work together.
  * Wide rivers should come as water POLYGONS (or set the width to about the river's width).
  * A closed lake OUTLINE drawn as a line excludes only the strip along the shore, not the lake: use the lake polygon.
  * The audit now reports line layers as INFO instead of ignoring them; the command line takes several `--exclude`
    GeoJSON files (lines and polygons) and `--line-width`.
  * The new field is the last input of the analysis (position 25), so no earlier position changes.

## What changed in v0.6.6

* **A standard data folder.** The new tool **0. Set Up and Audit Data Folder** (also `SETUP_DATA_FOLDER.bat`) creates
  `HLZ_DATA` with one sub-folder per input and a README in each:

  | Folder | Need | What goes there |
  |---|---|---|
  | `01_Elevation\DTM` | required | ONE bare-earth elevation raster |
  | `01_Elevation\DSM` | recommended | ONE surface raster (trees, buildings) |
  | `01_Elevation\Source_PointCloud` | optional | LAS/LAZ kept as source (not read) |
  | `02_Hydrology` | recommended | water polygons (lakes, rivers, reservoirs) |
  | `03_Exclusions` | optional | other no-landing areas: polygons, lines or points (wetland, built-up, restricted, known hazards) |
  | `04_AOI` | optional | ONE area-of-interest polygon |
  | `05_Search_Centres` | optional | point layers to search around |
  | `06_Reference` | not read | imagery, roads, original tiles, documents |
  | `07_Profiles` | optional | your own aircraft / mission / doctrine profiles |
  | `08_Runs` | output | analysis results |
  | `09_Audit` | output | audit reports |
  | `10_Obstacles` | recommended | towers, masts, wires, buildings: FAA DOF, CSV, GeoJSON or layers (v0.6.9) |

  Nothing is ever moved, renamed or deleted; running it again only adds what is missing.
* **The audit.** The same tool (or `AUDIT_DATA_FOLDER.bat`, or `python -m hlzcore.cli audit-data`) scans the folder and
  reports every finding as FAIL, WARN, INFO or PASS with what to do about it, for example: no DTM, several DTM tiles
  that need a mosaic, latitude/longitude or Web Mercator rasters, feet, integer heights, missing NoData, a DSM that is
  lower than the DTM (swapped or a different vertical datum), a DSM on another grid or covering only part of the DTM,
  river LINES instead of water polygons, layers that do not overlap the DTM, shapefiles without `.prj`, an AOI too close
  to the DTM edge for the approach range, invalid custom profiles, ZIP files not extracted, files in the wrong folder,
  low disk space and cloud-synced paths. It writes `09_Audit\HLZ_Data_Audit_latest.html` (plus a dated `.html`, `.txt`
  and `.json`) with the inputs the analysis will use, an extent diagram and an inventory.
  * It reads only file headers (GeoTIFF/BigTIFF tags, `.aux.xml` statistics, shapefile headers, GeoJSON, GeoPackage),
    so it takes seconds even for multi-gigabyte rasters, and it needs only the Python standard library. Inside ArcGIS
    Pro it also lists file geodatabases and reads `.img`/`.crf` through ArcPy.
* **Analyze from the data folder.** **1. Analyze HLZ Suitability** has a new first field, **HLZ data folder**. Choosing
  it fills the EMPTY fields: DTM, DSM, exclusion areas (all polygon layers in `02_Hydrology` and `03_Exclusions`), the
  AOI (when `04_AOI` holds exactly one) and the output folder (`08_Runs`). Fields you filled yourself are kept. The DTM
  field is now optional when a data folder supplies it. The command line has `--data-root`.
* The DTM check in the dialog now also refuses Web Mercator.
* Toolbox: the data folder is field 0 of the analysis, so every later position shifts by one.

## What changed in v0.6.5

* **Search around a point.** Instead of (or inside) an area of interest, give a **centre** and a **radius** and the tool
  looks for landing zones only inside that circle.
  * The centre can be a **point layer** (several points work; each gets its own circle) or a **typed position**: an MGRS
    grid reference (`18T VR 45906 30941`), decimal latitude/longitude (`45.4236, -75.7009`) or degrees, minutes, seconds.
  * With an AOI as well, only the overlap is searched. The circle needs data under it, plus the approach range around it.
  * Only the circle is analysed, so a small search finishes in seconds even on a very large DTM.
  * Every candidate reports its **distance and bearing from the centre** (`Distance_from_centre_m`,
    `Bearing_from_centre_deg`); equal-scoring sites are ordered nearest first. The circle is drawn on the report map and
    written as the layer `HLZ_Search_Area`. The command line has `--centre` (repeatable) and `--radius`.
* **The report explains itself.** A **?** next to every column heading says what the column is, what is good, and why it
  matters. A **How to read this table** panel repeats it with a short glossary. Every top site has an **In plain words**
  sentence. **Best approach** now reads `from 100° ESE`, then `land on 280° WNW, obstacle angle 1.2°`.
* **Pilot brief.** A button at the top of the report switches to a pilot view: one card per site (best 30) with
  position (MGRS, DMS, feet), how to come in, the highest obstacle, the directions that are open as bearing ranges,
  a rose with N/E/S/W, cautions and a **wind helper** (enter the wind and each site shows the best approach for it).
  It prints one site per page and can be downloaded as its own file; the same file is also written as
  `HLZ_Pilot_Brief.html`. Bearings are true.
* Toolbox: three new fields (centre layer, typed centre, radius) sit after the area of interest, so later positions shift.

## Documentation

Start with `docs\HLZ_Suitability_Quick_Start.html` (open it in a browser). The full guide is
`docs\HLZ_Suitability_User_Manual.docx`, and `docs\HLZ_Suitability_Data_Requirements.docx` lists the data and software
the tool needs, what is mandatory and what is optional, and how each affects accuracy.

## What changed in v0.6.4

* **The dialog warns when your data stops short of the approach range.** If the area (drawn polygon or map
  view) lies closer than the approach range plus the touchdown point and ring (1,045 m for the medium class)
  to an edge of the DTM, the dialog names the sides that are short, because approaches toward there cannot
  be fully checked and those candidates may be rated Unassessed.
* **The tool checks that the DSM and DTM agree.** On open ground, DSM minus DTM should be close to zero. If
  the lowest 5% of heights above ground is more than 0.5 m away from zero, or more than 5% of cells have a
  DSM below the ground, the run warns that the two rasters probably use different vertical datums or are
  misregistered. The measured offset is stored in `run_metadata.json` as `dsm_minus_dtm_p05_m`.
* **The CSV elevation is rounded to 0.01 m** (it showed values such as 105.50800323486328).
* Any polygon layer works as an exclusion area, so land-cover polygons (wetland, built-up) can be used too.

## What changed in v0.6.3

* **Water and other unsuitable surfaces can be excluded.** A new optional field, **Exclusion areas**,
  takes one or more polygon layers (lakes, ponds, wide rivers, reservoirs, wetlands). A touchdown point
  can never overlap them. Candidates within the cleared ring of one are annotated and report their
  distance to it (`Distance_to_exclusion_m`).
  * Without the layer, a lake is perfectly flat and smooth, so it passes every terrain test. The run now
    warns about this, and the report says "water and wetlands are NOT excluded".
  * Layers may be in any coordinate system and of any size; only the part near each tile is read.
  * Suggested sources: Ontario Hydro Network - Waterbody (Open Government Licence - Ontario), NRCan National
    Hydrographic Network, USGS NHD / 3DHP, OpenStreetMap water. Water layers can be old, so still check
    candidates on imagery.
* **The HTML report has an interactive map.** Candidates are drawn on a basemap with zoom, pan and a
  zoom-to-all button. Click an ID in the table (or **Show on map**) to zoom to that site; its touchdown
  point, cleared ring, 36 approach sectors and best approach are drawn. Click a marker to highlight its row.
  Rating filters hide markers and rows together. The map stays in view while you scroll the table.
  * The report is still one file (the map library is embedded). The basemap tiles (Esri imagery or
    topographic, OpenStreetMap) are fetched from the internet when the report is opened. That reveals the
    area being viewed, so the new **Basemap in the HTML report** option can build the report with
    **None**: it then makes no network requests at all.
* **Named aircraft: Canada, United States and NATO Europe.** 26 aircraft profiles are generated from one
  editable catalog (`profiles\aircraft\_catalog\rotorcraft_catalog.csv`) and appear in the aircraft list
  under their country group. Each record states its source and whether it was verified. See
  `profiles\aircraft\_catalog\README.txt`.
  * The Canadian types (CH-146, CH-147F, CH-148, CH-149) were verified against official RCAF pages.
  * US types use the touchdown-point sizes of US Army pathfinder doctrine (FM 3-21.38) as read from a
    training summary of it; European types use the weight class. Their dimensions are reference data that
    still need checking. The catalog contains **no restricted or classified doctrine**.
* Toolbox parameters changed: **Exclusion areas** is now field 5 and **Basemap in the HTML report** field 20;
  later positions shift. The smoke test is updated.

## What changed in v0.6.2

* **One command for all tests.** `RUN_ALL_TESTS.bat` runs the core, smoke, benchmark and
  (optional) command-line tests in the active environment, keeps going when a step fails, and
  writes a single zip of logs to send back.
* **Environment setup and check.** `SETUP_HLZ_ENV.bat` creates the `hlz` clone once.
  `tests\check_env.py` reports Python, packages, CPU, RAM and free disk, and flags:
  shared environments, a missing `arcpy`, low RAM, low disk and cloud-synced folders.
* **Cloud-sync warning in the dialog.** The Output folder field now warns when the path is
  inside OneDrive, Dropbox, Google Drive or similar, or is too long for Windows. Sync clients
  lock files and can corrupt a geodatabase, and city-scale runs write gigabytes.
* The engine is unchanged from v0.6.1.

## What changed in v0.6.1

* **No area limit.** Whole cities (Ottawa, the Greater Toronto Area) at 1 m can be analysed.
  * The area is processed **tile by tile**. Only one tile plus its 1 km approach buffer is in
    memory, so peak memory is set by the tile size (about 2.3 GB with the default 16 M-cell
    tiles), not by the area.
  * Results are cell-for-cell identical to an untiled run. Duplicate candidates on tile seams
    are removed.
* **Resume.** Each finished tile is checkpointed. If a long run is cancelled or Pro closes,
  run the tool again with the **same run name** and it continues from the last finished tile.
  The geoprocessing **Cancel** button works between tiles.
* **Progress and estimates.** The dialog shows the area, the number of tiles and a run-time
  estimate before you start. While running, a progress bar and the remaining time per tile
  are shown.
* **Field guidance.** Every field has a hover tooltip (the (i) icon) explaining what it is,
  what it requires and what it changes.
  * With "Explain each field" ticked (the default), each field also shows a context-aware
    note under it, prefixed `GUIDANCE:`.
  * Example: Hover mode explains OGE/IGE and says whether it will actually be used in this run.
  * Real problems (errors, warnings) always take priority over guidance.
* **New smart checks:**
  * a DSM that is the same raster as the DTM;
  * a DTM whose vertical unit is feet while Z units says Meters;
  * guidance on whether hover ceilings exist.
* **Large-area outputs:**
  * `Ground_Class` is always written, mosaicked from tiles.
  * Slope, roughness and obstacle rasters are written automatically only up to 100 M cells
    (set to ALWAYS to force them).
  * The HTML table lists the best 500 candidates; the CSV and geodatabase hold all of them.
* **Versioning.** Releases now use patch numbers: v0.6.1, v0.6.2, …

## Large areas: what to expect

| Area at 1 m | Cells | Tiles (default) | Time (estimate) | Peak memory |
|---|---|---|---|---|
| 10 x 10 km | 100 M | 9 | a few minutes | ~2.3 GB |
| Ottawa (~2,790 km2) | ~2.8 B | ~200 | about 1.5-4 h | ~2.3 GB |
| Greater Toronto Area (~7,125 km2) | ~7.1 B | ~500 | about 4-10 h | ~2.3 GB |

Times depend on the PC and the disk. Run `tests\benchmark_large_area.py` once to measure
your own speed (see Testing). To speed up large runs:

* Give the DSM the **same grid** as the DTM (same cell size and alignment). Otherwise each tile
  is resampled.
* Keep inputs on a local SSD.
* Choose "NEVER" for diagnostic rasters if you only need the candidates.

Disk use: about 1-3 GB of results per 1,000 km2.

## What changed since v0.3 (summary)

* **Runs inside ArcGIS Pro's own Python.** No second Conda environment, subprocess, GDAL,
  GeoPandas, Numba or extension licence is needed. The engine uses only NumPy, SciPy and ArcPy.
* **Readable results.** Every candidate gets a rating, a rank, an ID and a plain-language
  reason.
  * Ratings: Highly suitable, Suitable, Marginal, Unsuitable or Unassessed.
  * Ranks run best first, with IDs `HLZ-01`, `HLZ-02` and so on.
  * Each candidate states its main limiting factor in plain words.
  * Coordinates are given as MGRS and as latitude/longitude.
  * Map layers use traffic-light colours and labels. Best-approach lines point toward the LZ.
  * A one-page HTML report includes an approach "rose" for each top candidate.
* **Logic fixes.** The v0.3 issues fixed are:
  * A zero cleared ring with a DSM no longer rejects every cell.
  * Approaches running off the data edge are marked `INCOMPLETE` instead of looking clear.
  * The `row_col` truncation bug is fixed.
  * Upslope landings are now supported.
  * DSM voids no longer remove DTM cells.
* **Speed and scale.** FFT plane fitting with row tiling replaces the brute-force kernel, and
  approach analysis is vectorised.
  * The default cell limit rose from 250,000 to 10,000,000.
  * Measured in testing: a 2,400 x 2,400 m scene at 1 m runs in about 7 s.
* See `CHANGELOG.md` for the full list.

## Quick start in ArcGIS Pro

1. Unzip the release folder anywhere, for example `C:\GIS\HLZ_Suitability_Tool_v0.7.0`.
   Keep the folder structure intact: the `.pyt` loads `hlzcore\` and `profiles\` from its
   parent folder.
2. In ArcGIS Pro, open the **Catalog** pane, right-click **Toolboxes**, choose **Add Toolbox**,
   and select `toolbox\HLZ_Suitability.pyt`.
3. Optional but easiest: run **0. Set Up and Audit Data Folder** to create `C:\GIS\HLZ_DATA`, copy your DTM, DSM
   and water polygons into its sub-folders, and run it again to audit them. Then choose that folder in the first
   field of the analysis: the fields below are filled in for you.
4. Open **1. Analyze HLZ Suitability** and fill in:
   * **Bare-earth elevation (DTM)**: a single-band raster in a projected CRS in metres (e.g. UTM).
   * **Surface model (DSM)**: strongly recommended. Without it, trees and buildings are not
     assessed and ratings are capped at Marginal. A DSM on a different grid is resampled
     automatically.
   * **Area of interest**: a polygon. LZ centres are placed only inside it. The tool reads
     extra data around it so approaches can be checked. If you leave it empty, the current map
     view is used (option ticked by default). With neither, the whole DTM is analysed, which
     works only for DTMs under the cell budget.
   * **Aircraft profile**: pick from the list, or choose *Custom profile file...*.
   * **Output folder** and **Run name**: a sub-folder is created for each run, and earlier runs
     are never overwritten.
5. Run the tool. The results are added to the current map as a group layer, and the HTML
   report opens.

No packages need to be installed for the toolbox itself, because ArcGIS Pro's default
environment already contains everything required.

## Outputs (per run folder)

| Output | What it shows |
|---|---|
| `HLZ_Results.gdb\HLZ_Footprints` | TDP circles coloured by rating, labelled `ID (score)`; all attributes have readable aliases |
| `HLZ_Results.gdb\HLZ_Cleared_Ring` | Outer cleared-ring outline |
| `HLZ_Results.gdb\HLZ_Best_Approach` | Line from the start of the best approach to the LZ, labelled with landing heading and obstacle angle |
| `HLZ_Results.gdb\HLZ_Approach_Sectors` | Every sector for the top 10 candidates (clear / steep / blocked / no data), hidden by default |
| `HLZ_Results.gdb\HLZ_Overview_Grid` | Optional (v0.7.0): coarse cells with slope, roughness, elevation range, share passing the ground test and candidate count; coloured by `Pass_Class` |
| `HLZ_Results.gdb\Ground_Class` | 1 = passes for any heading, 2 = upslope landing only, 0 = fails |
| `Slope_deg`, `Roughness_m`, `Obstacle_Height_m` | Supporting rasters, hidden by default; all rasters cover the AOI plus a ~50 m halo |
| `HLZ_Report.html` | Offline report: summary, overview map, ranked table, per-candidate approach roses, warnings, limitations |
| `HLZ_Candidates.csv` | Same table, ready for Excel; the header is the first row |
| `run_metadata.json` | Full provenance: inputs, profiles, options, every sector, warnings |
| `hlz_run.log` | Step-by-step run log |

### How to read a candidate

* **Rating** is what the evidence supports. **Status** says whether the thresholds behind it
  are approved doctrine (`Approved profile`) or defaults (`Planning only`).
* **Approach from** is the true bearing from the LZ toward the approach path.
  **Landing heading** is the heading flown on final.
* **Approach obstacle angle** is the steepest angle from the clearance height above the LZ to any
  terrain or obstacle in the sector.
  * Clear sector: at or below `suitable_angle_deg` (3 degrees by default).
  * Blocked sector: above `unsuitable_angle_deg` (7 degrees by default).
* **Upslope landing only**: the slope exceeds the general limit but not the upslope limit. Only
  approaches that line up with the uphill heading are considered.
* **Unassessed**: the data does not cover enough of the approach area. Extend the DTM/DSM by
  the aircraft's `approach_range_m` beyond the AOI.

## Profiles

Profiles are JSON files in `profiles\`. Copy one before editing it, then check the copy with the
**2. Validate Profiles** tool.

* `aircraft\`: TDP diameter, cleared ring, slope, roughness and obstacle limits, approach range,
  corridor half-width, glide-angle thresholds (3/7 degrees by default), clearance height and
  optional hover ceilings.
  * Shipped: generic light, medium and heavy. All are planning defaults.
* `missions\`: scoring weights, sampling, candidate count and coverage thresholds.
  * `max_input_cells` is the terrain-analysis budget for the AOI (about 60 bytes per cell).
  * `max_surface_cells` is the read-window budget, including the approach buffer (about 10
    bytes per cell).
* `doctrine\`: score thresholds and approval status. Set `"approval_status": "approved"` only
  after the responsible authority has approved every threshold and aircraft value.

YAML profiles from v0.3 still load if PyYAML is available.

## Testing (please return the logs)

**One environment for all tests.** The tests run in a dedicated clone of ArcGIS Pro's Python,
named `hlz`. Never install or test in `base` or `arcgispro-py3`. The clone has `arcpy`, NumPy,
SciPy and matplotlib, which is everything the required tests need.

Open the ArcGIS Pro **Python Command Prompt** (Start menu, ArcGIS folder; not a regular
Anaconda Prompt, which has no `arcpy`).

**Once:** create the environment. If `hlz-arcpy-v04` from earlier versions exists, it is cloned
(fast); otherwise `arcgispro-py3` is cloned (a few GB, a few minutes). Nothing is installed.

```text
cd /d "C:\GIS\HLZ_Suitability_Tool_v0.7.0"
SETUP_HLZ_ENV.bat
```

You can also keep using `hlz-arcpy-v04`: every script accepts any dedicated clone.

**Every time:**

```text
activate hlz
cd /d "C:\GIS\HLZ_Suitability_Tool_v0.7.0"
RUN_ALL_TESTS.bat
```

`RUN_ALL_TESTS.bat --quick` uses a 3 km benchmark instead of 10 km. Each step runs separately,
so one failure never hides the others. Send back **one file**:
`logs\hlz_test_logs_v0.7.0.zip`. It holds a summary, an environment report (CPU, RAM, disk,
package versions), every test log and the console output of each step.

| Step | What it tests | Needs |
|---|---|---|
| core | 82 unit tests (profiles, terrain, tiling, resume, exclusion, obstacles, report map, aircraft catalog, scoring, guidance, data folder and audit) | NumPy, SciPy |
| smoke | The real toolbox through 10 scenarios (A-J): DTM + DSM + AOI, DTM only, a DSM on a 2 m grid, many tiles with an AOI, many tiles over the whole DTM, a lake that must be excluded, a point-and-radius search, a data folder (tool 0 audit, then an analysis given only the folder) a river line widened to 20 m, and listed obstacles (a layer in feet, a CSV and a GeoJSON file) | `arcpy` |
| benchmark | Speed and peak memory on a simulated area (`--km 10` = 100 M cells), plus Ottawa and GTA time estimates | NumPy, SciPy |
| cli | Optional command-line end-to-end test (139 checks, including obstacle files, `init-data`, `audit-data` and `analyze --data-root`) | `rasterio` |

The cli step is **skipped automatically** when `rasterio` is not installed. Do not install it
into the ArcGIS clone: `rasterio` brings its own GDAL build, and mixing it with Esri's can
break `arcpy`. To run the command-line tool, use the separate environment instead:

```text
conda env create -f environment.yml
conda activate hlz-cli-v04
RUN_CLI_END_TO_END_TEST.bat
python -m hlzcore.cli analyze --dtm C:\data\dtm.tif --dsm C:\data\dsm.tif --output C:\data\hlz --oat-c 25
```

Individual tests are still available: `RUN_CORE_TESTS.bat`, `RUN_ARCPY_SMOKE_TEST.bat`, and
`python tests\benchmark_large_area.py --km 10`. The benchmark estimate needs at least 3 km; at
1 km the fixed overhead makes it far too pessimistic.

`python tests\check_env.py` checks the active environment on its own and explains what to fix.

Map styling needs the ArcGIS Pro user interface. Check it once by running the tool from its
dialog with **Add styled results to the current map** ticked.

## Known limitations

* Wires, thin poles and small obstacles are usually missing from DSMs.
* Threat exposure, surface bearing strength, brown-out/white-out and live weather are not
  assessed.
* Density altitude uses rule-of-thumb formulas; hover ceilings must come from operator data.
* The `Ground_Class` raster is a fast screening surface. Every final candidate is re-checked
  exactly (maximum plane residual and true-disk obstacle heights), so it may differ slightly
  from the raster at the margins.

## License

SPDX-License-Identifier: GPL-2.0-or-later
Copyright (c) 2026 Eui Soo SON
