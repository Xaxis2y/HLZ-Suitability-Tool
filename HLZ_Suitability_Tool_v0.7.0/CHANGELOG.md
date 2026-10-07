<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- Copyright (c) 2026 Eui Soo SON -->

# Changelog

## v0.7.0 (2026-10-07)

Request: take what is good from SlopeRuggednessTool-Santos and use it to improve the HLZ Suitability Tool.

* New `hlzcore/overview.py`: terrain overview grid. Per-tile sums (valid cells, slope sum/max, roughness sum, passing and
  upslope-only cells, elevation min/max) are reduced to overview cells by padding each tile to the block grid, so tile
  edges need not fall on block boundaries; the partials are stored in the tile checkpoint (`stats["overview"]`) and added
  up at the end. `finalize` derives means, percentages, `Terr_Class` and `Pass_Class`; `attach_candidates` adds
  `Cand_Count` and `Best_Rating`. A cell is at least 16 analysis cells; sizes from 20 m to 20 km are accepted.
* Pipeline: `RunOptions.overview_cell_m` (None or 0 = off); `analyze_tile` returns the partial; `run_tiled_pipeline` builds
  the overview after ranking and calls `writer.write_overview` when the adapter has it (otherwise a warning); metadata
  block `overview`. The overview cell size is part of the resume signature through the options.
* `masks.rasterize_points`: circle of a given radius around each point (a radius of 0 marks the cell that holds the
  point; points just outside the grid still count).
* Exclusion areas accept points: ArcPy reader (`Point`/`Multipoint` layers, new `point_radius_m`), rasterio reader (GeoJSON
  `Point`/`MultiPoint`), description and resume digest include the radius, command line `--point-radius`.
* Writers: `ArcpyWriter.write_overview` (feature class `HLZ_Overview_Grid`, 16 aliased fields), `RasterioWriter.write_overview`
  (`HLZ_Overview_Grid.geojson`); the map gets the layer styled by `Pass_Class`, under the other results.
* Toolbox: new inputs 32 (point radius), 33 (create overview grid), 34 (overview cell size); derived outputs move to 35-36.
  Exclusion filter now lists Point and Multipoint; guidance and tooltips added; metadata XML rebuilt.
* Data folder: `03_Exclusions` accepts point layers (INFO in the audit; used by the analysis); `02_Hydrology` does not.
  Fixed `_overlap` for vector layers: a layer with a single point (zero-size extent) inside the DTM is no longer reported as
  "does not overlap the DTM".
* Command line: `--overview-cell`, `--point-radius`.
* Not carried over from the other tool, on purpose: its standard-deviation "ruggedness" (mixes slope with roughness), the
  mean slope per large cell as a suitability test, the ad-hoc score `slope + ruggedness + range/10` and circle placement
  at grid-cell label points. This tool's plane-fit roughness, scoring and exact re-check are better.
* Already present in v0.6.9 and therefore not added: the pre-run size and run-time estimate in the dialog.
* Not verified here: everything that needs ArcGIS Pro (the new fields, point-layer reading, the overview feature class and
  its styling, ArcPy smoke scenario K).
* Tests: 99 unit tests (17 new in `tests/test_overview.py`, 2 updated in `tests/test_data_root.py`), 163 command-line checks
  (scenario `with_hazards_overview`), ArcPy smoke scenario K (11 scenarios in all). The User Manual and Data Requirements
  documents (.docx) were not rewritten.

## v0.6.9 (2026-10-05)

Request: adapt obstacle data types (FAA DOF, CSV, GeoJSON / OpenStreetMap, GIS layers) so a communication tower in the
glide slope is no longer shown as green.

* New `hlzcore/obstacles.py`: readers for FAA DOF (`.dat`, fixed-width, dismantled records skipped), CSV (latitude /
  longitude / height columns, many column names), GeoJSON (OpenStreetMap `height` and `building:levels`); height parsing
  with units (`100 ft`, `_ft` fields, a units setting); an `ObstacleSet` that rasterises footprints (point = disk, line
  = widened, polygon = fill + edge) and builds exact vertical samples for the approach test.
* Pipeline: footprints are burned into the surface as `max(DSM, DTM + AGL)` (also without a DSM, where only the approach
  analysis uses them); cells within the touchdown radius (+ ring for the ring limit) of a footprint taller than the limits
  are removed; every approach sector is tested analytically against each listed obstacle (a thin mast between two rays
  is found); the limiting obstacle is named in the sector. Obstacles are part of the resume signature.
* Readers: ArcPy (`obstacle_layers`, `obstacle_files`; layers read with a cursor in the DTM CRS, files projected with the
  best datum transformation) and rasterio (`obstacle_paths`). Obstacles up to about 5 km outside the DTM are read.
* Toolbox: six new inputs (26-31): obstacle layers, obstacle files, height field, height units, default height (30 m),
  buffer (10 m); the derived outputs move to 32-33. Guidance and tooltips added; the tool metadata was rebuilt.
* Data folder: new `10_Obstacles` (RECOMMENDED); the audit checks coverage of the DTM, counts records, warns about heights
  that will be assumed and about a missing list; `analyze --data-root` and the toolbox fill the obstacle fields from it.
* Command line: `--obstacles`, `--obstacle-height-field`, `--obstacle-units`, `--obstacle-default-height`, `--obstacle-buffer`.
* Outputs: `Nearest_obstacle_m`, `Nearest_obstacle` in the CSV (`Obst_Dist_m`, `Obst_Name` in the geodatabase); purple
  obstacle dots on the report map; listed-obstacle lines in the report, pilot brief and plain summary; metadata block
  `obstacles` (description, count, assumed heights, markers); updated limitations text.
* Not verified here: the FAA DOF layout against a real file (tested on a synthetic file written from the FAA description),
  and everything that needs ArcGIS Pro (the new fields, layer reading, scenario J of the smoke test).
* Tests: 82 unit tests (8 new), 139 command-line checks (DOF, CSV, GeoJSON, no-DSM scenarios), ArcPy smoke scenario J (10 scenarios in all).

## v0.6.8 (2026-10-05)

Request: choose the helicopter type from a drop-down so the HLZ ring size follows the aircraft.

* The existing **Aircraft profile** drop-down (generic light / medium / heavy plus 26 named Canadian, US and NATO types)
  now shows each type's size in its label, e.g. "United States: CH-47F Chinook - 80 m landing point + 30 m ring".
  The profile file name stays last in brackets, so the run name is unchanged.
* The field guidance under the drop-down states the selected type's landing point, ring and the resulting analysis
  radius (landing point / 2 + ring). Sizes still come from the profile; "Custom profile file..." is unchanged.
* Tests: 74 unit tests (1 new: labels, sizes and guidance).
* Documentation brought up to v0.6.8: the User Manual and the Data Requirements document now cover the data folder and
  audit (new Manual Section 4.4, Requirements Section 3.8), line exclusions and the line width (Manual Section 4.5), the
  helicopter list with sizes, the new tool 0, new troubleshooting rows, test counts, the glossary and the release
  folder layout; the contents lists were rebuilt. README and Quick Start were updated too.

## v0.6.7 (2026-10-05)

Request: allow water LINE shapefiles (rivers, streams) as well as polygons.

* `masks.rasterize_lines`: marks every cell a line passes through (sampled every half cell) and widens it to a total
  width, using a padded grid so a line just outside a tile still counts. Closed rings are not filled.
* Exclusion layers may be polygons or lines in the ArcPy reader (`Polyline` layers; part by part) and in the GeoJSON
  reader (`LineString`, `MultiLineString`); several GeoJSON files can be combined. The width is part of the resume
  signature and is recorded in `run_metadata.json` ("lines widened to N m").
* Toolbox: the exclusion field accepts Polygon and Polyline; new field **Width given to exclusion LINES (m)** (default
  10, 0-500), appended as the last input (position 25) so earlier positions are unchanged; guidance explains lines.
* Data folder audit: lines in `02_Hydrology`/`03_Exclusions` are used (INFO about the width) instead of a WARN; points are
  still refused; the folder READMEs say so. Command line: repeatable `--exclude`, `--line-width`;
  `analyze --data-root` passes every GeoJSON exclusion layer.
* Tests: 73 unit tests (2 new), 114 command-line checks (river line scenario, data folder with a river), ArcPy smoke scenario I.

## v0.6.6 (2026-10-05)

Request: make it easy to start - create a data root folder with a place for the DTM, DSM, hydrology and other inputs,
and scan it to audit whether the needed data is there.

### Data folder
* New `hlzcore/data_root.py` (Python standard library only): the layout (`01_Elevation\DTM`, `DSM`, `Source_PointCloud`,
  `02_Hydrology`, `03_Exclusions`, `04_AOI`, `05_Search_Centres`, `06_Reference`, `07_Profiles`, `08_Runs`, `09_Audit`),
  a `hlz_data_root.json` marker and a README per folder. Creation only adds; READMEs the user edited are kept.
* Header readers without GDAL: classic and BigTIFF GeoTIFF (both byte orders; size, cell, extent from tie point and
  scale or transformation, PixelIsPoint, data type, compression, tiling, NoData, GeoKeys incl. vertical CS),
  `.aux.xml` statistics/SRS, `.prj`/WKT1/WKT2, world files, VRT, ESRI ASCII grid, shapefile (`.shp`/`.dbf`/`.prj`),
  GeoJSON (with bounding box) and GeoPackage feature tables. Cross-checked against GDAL 3.12 on 84 GeoTIFF variants
  (7 coordinate systems incl. compound, 6 encodings, float and integer): no differences.
* Audit checks for the DTM, DSM, hydrology, exclusions, AOI, search centres, profiles, point clouds, archives,
  misplaced files, the path and the free disk space, each with an action. Status READY, READY WITH WARNINGS or NOT
  READY. Reports in `09_Audit` (HTML with extent diagram and level filters, TXT, JSON). Geographic layers are compared
  with a UTM DTM by projecting their bounding box with the tool's own UTM formulas.
* ArcGIS Pro: new tool **0. Set Up and Audit Data Folder**; `io_arcpy.audit_inspector` adds file geodatabases, mosaic
  datasets and other ArcGIS rasters. **1. Analyze HLZ Suitability** gets the **HLZ data folder** field (field 0) that
  fills empty fields (DTM, DSM, exclusions, AOI, output folder); the DTM field is optional when the folder supplies one.
* Command line: `init-data`, `audit-data` and `analyze --data-root` (`--dtm` and `--output` default from the folder);
  `python -m hlzcore.data_root init|audit` runs without NumPy. `SETUP_DATA_FOLDER.bat` and `AUDIT_DATA_FOLDER.bat`.
* The dialog's DTM check also rejects Web Mercator (distances stretched by about 41% at 45 degrees north).

### Tests
* 15 new unit tests (`tests/test_data_root.py`, run by `RUN_CORE_TESTS.bat`; 71 in total), 8 new command-line checks
  (93 in total) and ArcPy smoke scenario H (tool 0 on an empty and a filled folder, the header reader against ArcGIS,
  and an analysis given only the data folder).


## v0.6.5 (2026-10-05)

Requests: look for landing zones around a point with a radius; make the report understandable to someone who knows
nothing about helicopters; add a report for the pilot.

### Search around a point
* New `hlzcore/search.py` (pure NumPy): `SearchArea` (window, mask and nearest-centre distance and bearing),
  `parse_search_centre` (MGRS, decimal latitude/longitude with hemisphere letters, degrees-minutes-seconds) and the
  inverse MGRS and UTM conversions. The MGRS inverse agrees with the independent `mgrs` library on 4,000 random
  global points (within the 0.7 m half-cell) and round-trips through the tool's own forward conversion.
* All three readers accept a search area; the analysis window shrinks to the circle's bounding box and tiles outside
  every circle are skipped. A circle combined with an AOI polygon searches the overlap. The circle is part of the
  resume signature.
* New candidate fields `search_distance_m` and `search_bearing_deg` (CSV `Distance_from_centre_m`,
  `Bearing_from_centre_deg`, geodatabase `Dist_Ctr_m`, `Brg_Ctr`). They break ties in the ranking (nearest first).
* New layer `HLZ_Search_Area` (geodatabase) or `HLZ_Search_Area.geojson`; the circle and centre are drawn on the
  report map and included when zooming to all.
* Toolbox: three fields (point layer, typed centre, radius) with validation (bad text, centre without radius, radius
  without centre), an area and time estimate for the circle, and the data-extent warning applied to the circle.
  CLI: `--centre` (repeatable) and `--radius`. ArcPy position conversion uses the best datum transformation.

### A report anyone can read
* New `hlzcore/report_text.py`. Every column heading has a **?** tooltip (what it is, what is good, why it matters),
  built from the aircraft and mission profile in use, so the limits quoted are the real ones. A **How to read this
  table** panel and a glossary sit above the table. Each detail card starts with a generated sentence. The **Best
  approach** cell spells out the direction to come from, the heading to land on and the obstacle angle.

### Pilot brief
* A view switch (Planner report, Pilot brief, Print) at the top of `HLZ_Report.html`; cards for the best 30 sites
  (position in MGRS and DMS, elevation and density altitude in feet, landing area, slope or uphill warning, the
  approach, the highest obstacle on it in feet with its distance and bearing, open, steep, blocked and no-data
  directions as bearing ranges, a rose, cautions). A wind helper chooses the best approach for any wind and reports the
  head and cross wind. One site per printed page. **Download as a file** and the new `HLZ_Pilot_Brief.html` give the
  same brief stand-alone. Bearings are true; the tool does not know the magnetic variation.

### Tests
* 56 unit tests (14 new), 85 command-line checks (scenarios with a latitude/longitude and an MGRS centre) and ArcPy
  smoke scenario G (typed centre, no AOI, many tiles). A developer script, `tests/dev_check_report_ui.py`, drives the
  report in a headless browser (tooltips, view switch, wind helper, site count, download).


## v0.6.4 (2026-10-05)

Small accuracy and usability fixes found while documenting v0.6.3.

* **Data-extent warning restored in the dialog.** The v0.6.1 tiling rewrite had dropped it. The warning now names
  the sides of the DTM that lie closer than the approach range plus the touchdown point and ring.
* **DSM minus DTM consistency check.** Per tile, the 5th percentile of DSM minus DTM (every 5th cell) must be
  within 0.5 m of zero, and fewer than 5% of cells may be more than 0.5 m below the ground. One warning is raised
  for the whole run; the median offset is written to the metadata. Tested with +2 m and -2 m shifts. Before, a
  vertical-datum mismatch went undetected.
* **CSV and geodatabase elevation rounded to 0.01 m.**
* Tests: 42 unit tests (2 new).
* **Documentation** in `docs\`: a one-page Quick Start (HTML), a User Manual (Word, 46 pages) and a Data Requirements and
  Compatibility document (Word, 14 pages; must-have, recommended and optional data, public sources checked against provider
  pages, compatibility matrix). The manual's page numbers were filled from a rendered copy.


## v0.6.3 (2026-10-05)

Requests: a map with the report overview, zoom, and click-to-zoom from the table; keep lakes out of the
results; and named Canadian, US and NATO helicopter data.

### Exclusion areas (hydrology)
* New `hlzcore/masks.py`: exact even-odd scanline polygon rasteriser (islands are holes; cell centres decide)
  and a distance transform. Verified cell-for-cell against matplotlib; a 3,000-vertex lake fills a 16 M-cell
  tile in 0.15 s.
* A new `exclusion_mask()` on every reader (ArcPy `SearchCursor` with a spatial filter, per tile; Rasterio from
  GeoJSON). `analyze_tile` fails every ground cell whose distance to an excluded cell is within the touchdown
  radius, so a touchdown point can never touch an exclusion polygon. The quality surface therefore also prefers
  centres away from water.
* New candidate field `excluded_distance_m` (CSV `Distance_to_exclusion_m`, geodatabase `Excl_Dist_m`), reported
  within 3 x (touchdown radius + ring); candidates inside the cleared ring get a note.
* Without a layer the run warns that water and wetlands are not excluded. The exclusion layers are part of the
  resume signature.
* Toolbox: multi-value polygon field with shape-type and overlap checks, guidance, tooltip. CLI: `--exclude`
  (GeoJSON) and `--exclude-crs`.
* Tests: a lake in the synthetic scene. Without the layer 9 candidates sit on it; with it, none do and the
  nearest is 30.2 m away (radius 25 m). Tiled and untiled ground rasters are identical with an exclusion layer.

### Interactive report map
* New `hlzcore/report_map.py` embeds Leaflet 1.9.4 (BSD-2-Clause; licence file included) and the candidate data.
* Basemaps: Esri imagery (default), Esri topographic, OpenStreetMap. Tiles load from the internet when the
  report is opened; this was **not** tested against the live servers (the development environment has no
  internet), only the tile URLs and the page logic.
* New option **Basemap in the HTML report** (and CLI `--basemap`): OFFLINE builds a report with no tiles and no
  network requests (verified: 0 external requests in a headless browser).
* The map is sticky above the table; ID links, **Show on map** buttons, marker clicks, rating filters,
  zoom-to-all. The old SVG overview is kept in a collapsed section. The input boxes moved below the table.
* Tested in headless Chromium: no script errors, 60 of 60 markers and rows, zoom and centring exact
  (0.0 m offset), selection both ways, filters.

### Aircraft catalog
* `profiles/aircraft/_catalog/rotorcraft_catalog.csv` (26 aircraft) and `scripts/build_aircraft_profiles.py`.
  Weight class (light up to 4,500 kg, medium up to 14,000 kg, heavy above) selects the generic class limits;
  the touchdown-point size comes from the catalog (US doctrine categories where listed).
* Each record has `data_status` and `source`. Canadian records were checked against canada.ca; all others are
  marked as reference data.
* `list_profiles` now reads sub-folders, groups them (Generic, Canada, United States, NATO Europe) and ignores
  folders that start with an underscore.
* Tests: every catalog row has a valid profile, a touchdown point of at least 1.5 x the aircraft footprint, and
  a source; Canadian rows are verified.

### Other
* 40 unit tests, 51 command-line checks. A new ArcPy smoke scenario F excludes a lake and measures the nearest
  touchdown point with `Polygon.distanceTo`.


## v0.6.2 (2026-10-05)

Request: one environment for all testing. The engine is unchanged from v0.6.1 (apart from
the version string).

* **`tests/run_all_tests.py` and `RUN_ALL_TESTS.bat`.**
  * Run core, smoke, benchmark and cli as separate processes in the active environment.
  * A crash or failure in one step does not stop the others, and each step has a time limit.
  * Output is streamed live and saved. The result is `hlz_test_summary_<v>.txt`, an environment
    report, every step log and console file, bundled in `logs\hlz_test_logs_<v>.zip`.
  * Options: `--steps core,smoke`, `--quick` (3 km benchmark), `--km N`, `--force`.
  * Exit code 0 only if every executed step passed; skipped steps do not count as failures.
  * It refuses to start in a shared or incomplete environment unless `--force` is given.
* **`tests/check_env.py`.** The environment report and rules:
  * errors: `base` or `arcgispro-py3`, Python older than 3.9, missing NumPy or SciPy, and
    `arcpy` missing when the smoke test is planned;
  * warnings: low RAM (under 8 GB), low disk (under 10 GB), cloud-synced tool folder, and
    `rasterio` missing for the optional cli step.
* **`SETUP_HLZ_ENV.bat [name]`.** Creates the clone `hlz` once, from `hlz-arcpy-v04` if it
  exists, otherwise from `arcgispro-py3`. It then runs the environment check. Existing
  environments are left alone, and nothing is installed or removed. The script is untested on
  Windows (written and syntax-checked on Linux).
* **Output-folder warning in the dialog** (`help_text.path_warning`): cloud-synced folders and
  paths over 120 characters. The guidance for the field now states the disk requirement.
* Smoke-test documentation now describes the 5 scenarios (A-E).
* Tests: 32 unit tests (3 new: path warning, environment rules, summary extraction).
* Note: the version is part of the resume signature, so a run started with v0.6.1 will not be
  resumed by v0.6.2; it starts a new numbered folder.

## v0.6.1 (2026-10-05)

Request: load very large areas such as Ottawa or the Greater Toronto Area at 1 m (billions of
cells), and explain every field in the dialog. Versioning now uses patch numbers.

### Out-of-core tiled processing (no area limit)

* The new `hlzcore/tiling.py` contains:
  * `plan_tiles` (non-overlapping, near-equal tiles);
  * `TileCheckpoint` (per-tile JSON, atomic writes, signature-checked resume);
  * candidate (de)serialisation;
  * `suppress_across_tiles` (spatial-hash removal of duplicates at tile seams);
  * `checkpoint_status`.
* `pipeline.run_tiled_pipeline` replaces the single-window pipeline. Per tile it:
  1. reads the tile plus the approach buffer;
  2. analyses terrain on the tile plus a halo;
  3. extracts candidates (`max_candidates_per_tile`, default 100);
  4. runs approaches and scoring;
  5. writes the tile rasters;
  6. writes a checkpoint.
* Globally it then removes seam duplicates, applies the `max_candidates` cap (default 1,000,
  up to 100,000), ranks, converts coordinates, writes the vectors and writes the reports.
* `run_pipeline(InputData)` remains as an in-memory wrapper.
* `max_input_cells` now means cells per **tile** (memory control, default 16 M, about 2.3 GB
  peak). It never limits the total area. `max_surface_cells` is no longer used.
* Readers:
  * `ArcpyTileReader` reads windows on demand. With an AOI polygon, tiles outside it are
    skipped, tiles fully inside need no mask, and only edge tiles are rasterised.
  * `RasterioTileReader` reads windows and aligns a mismatched DSM on the fly through a
    `WarpedVRT`.
* Writers:
  * The ArcPy writer saves tile GeoTIFFs and then runs `MosaicToNewRaster` into the
    geodatabase (NoData, statistics, attribute table, pyramids).
  * The Rasterio writer writes windows into one tiled BigTIFF and reopens it when resuming.
* `Ground_Class` cells outside the AOI are now NoData (transparent) instead of 0 (fail).
* Diagnostic rasters: new `AUTO`/`ALWAYS`/`NEVER` option. AUTO skips them above 100 M cells.
* Toolbox:
  * area/tile/run-time estimate in the dialog;
  * tile progress bar with time remaining;
  * Cancel works between tiles;
  * re-running the same run name resumes an unfinished run;
  * a checkpoint from different inputs falls back to a new numbered folder;
  * the "Maximum number of candidates" range is now 1-100,000.
* The HTML report lists the best 500 candidates (all are in the CSV and geodatabase). The
  metadata JSON lists the first 1,000, with approach sectors for the top N.

### Field guidance and smart checks

* `hlzcore/help_text.py` is the single source of field explanations: what the field is, what
  it requires, its effect, and an example.
* Hover tooltips:
  * `scripts/build_tool_metadata.py` generates `toolbox/HLZ_Suitability.AnalyzeHLZ.pyt.xml`,
    `…ValidateProfiles.pyt.xml` and `HLZ_Suitability.pyt.xml`.
  * These provide ArcGIS Pro's (i) tooltip for every parameter (21 and 3), plus the tool
    summary and usage.
* Dialog guidance:
  * New parameter "Explain each field in the dialog (guidance)", index 18, default on.
    Derived outputs move to 19 and 20.
  * Shows context-aware `GUIDANCE:` notes under each field, only where no real error or
    warning is present.
  * Example: Hover mode explains OGE/IGE and states whether it is used in this run, and why
    not.
* New checks:
  * DSM identical to the DTM (error);
  * DTM vertical unit in feet while Z units is Meters (warning).
* Corrected a density-altitude example in the help text. A 500 m LZ at 30 C is about 3,830 ft,
  not 8,900 ft; the help now uses a verified Ottawa example.

### Tests

* There are now 29 unit tests. The new ones check:
  * that the tiles cover the area exactly once;
  * that the tiled rasters are identical to a single-tile run (all four rasters);
  * that there are no duplicates across tile seams;
  * that a resumed run is identical to an uninterrupted one;
  * that resume refuses different inputs;
  * that tiles outside the AOI are skipped;
  * that a large area raises no size limit;
  * the hover guidance states and the help-entry completeness.
* New `tests/benchmark_large_area.py`: a procedural reader for any area size. It reports
  throughput, peak memory and Ottawa/GTA time estimates.
* Smoke scenarios D and E now use 300 k-cell tiles. E analyses the whole 5.76 M-cell DTM with no
  AOI and must **succeed** in several tiles. The valid-cell checks are AOI-aware.

## v0.6 (2026-10-04)

Triggered by a real 1 m DTM of 10,000 x 10,000 cells (100 M). The dialog refused it with "The
analysis window has 100,000,000 cells (limit 10,000,000)".

* **Two budgets.** `max_input_cells` (10 M) now applies only to the terrain-analysis core: the
  AOI bounding box plus a halo of `max(outer radius, 2 x TDP radius)` cells. The new
  `max_surface_cells` (150 M) applies to the full read window, AOI plus approach buffer, which
  is only sampled along approach rays.
  * At 1 m cells, the largest AOI grows from about 1.1 km to about 3.1 km across.
  * Terrain values inside the AOI are identical to a full-raster run (unit-tested).
* **Map-view AOI.** New parameter "If no AOI is given, use the current map view extent",
  default on.
  * When no AOI is drawn, the active map view extent, projected to the DTM CRS, becomes the
    AOI.
  * New function `io_arcpy.view_extent_polygon`. `load_inputs` accepts either a layer or a
    polygon geometry.
* **Clearer limit messages.** The dialog and pipeline now report the maximum AOI size at the
  current cell size, and say whether the whole DTM, the AOI or the map view is too large.
* Rasters are written for the core window only (`OutputWriter.set_raster_grid`), so they are
  smaller and faster to write.
* `io_arcpy._read_window` reads float rasters straight into float32 with no float64
  temporary, which halves peak memory for large windows.
* The candidate-extraction nDSM is computed on the core window only.
* Mission profiles gain `max_surface_cells`. Older profiles without it default to 150 M.
* Toolbox parameter order changed: `use_map_view` is inserted at index 3, and later indices
  shift by one. Scripts calling `arcpy.AnalyzeHLZ_hlz` positionally must add it.
* Tests:
  * New unit tests `test_core_window_terrain_matches_full_run` and
    `test_two_budgets_core_small_read_window_large`, bringing the total to 21.
  * Smoke scenarios D (tight budget with an AOI, must pass) and E (same budget without an AOI,
    must be refused with a size hint).
  * A check that the output rasters cover only the AOI core.

## v0.5 (2026-10-04)

Fixes found by the first ArcGIS Pro 3.7.2 smoke test (core 18/18 and smoke 37/37 passed,
but the log showed 5,760,000 valid cells where 5,759,039 were expected).

* **NoData read as elevation (ArcPy path).** `io_arcpy._read_window` compared cells to
  `raster.noDataValue` by exact float equality. A file geodatabase stores float NoData as
  about -3.4e38, which ArcPy reports with different rounding, so NoData cells passed as
  valid terrain.
  * Floating rasters are now read with `nodata_to_value=NaN`.
  * Integer rasters are compared in their own dtype.
* **Elevation plausibility guard** (`pipeline.plausible_elevation_mask`). Cells outside
  -1,000 to 9,000 m are treated as NoData, with a warning. This catches undeclared sentinels
  such as -9999 and -32767, as well as wrong Z units. It is applied in the ArcPy adapter and
  again in the pipeline for every adapter.
* Adapter notes (DSM resampled, AOI buffer short, NoData guard) are now logged as warnings, so
  they reach the Geoprocessing pane. Previously they only reached the report and metadata.
* Smoke test asserts:
  * the exact valid-cell count, which proves the NoData hole is masked;
  * the DSM-resampling warning in scenario C.
* The fake `arcpy` now emulates the file-geodatabase NoData rounding, so the bug reproduces
  in the sandbox: v0.4 gave 5,760,000 and v0.5 gives 5,759,039.
* New unit test `test_plausibility_mask_drops_sentinels`, bringing the total to 19.
* Conda environment names are unchanged (`hlz-arcpy-v04`, `hlz-cli-v04`), so existing clones
  can be reused.

## v0.4 (2026-10-04)

### Architecture

* The toolbox now runs entirely in ArcGIS Pro's Python (`arcgispro-py3`). v0.3 needed a separate
  Conda environment with GDAL, Rasterio, GeoPandas and Numba, launched as a subprocess.
  v0.4 needs only NumPy, SciPy and ArcPy.
* GIS input and output were split into adapters:
  * `io_arcpy` for the toolbox;
  * `io_rasterio` for the optional CLI.
* A shared `pipeline.run_pipeline` orchestrates both, so the CLI tests exercise the same logic
  as the toolbox.
* Profiles are JSON, using only the standard library. v0.3 YAML profiles still load if PyYAML
  is present.

### Logic fixes

* A `cleared_ring_m` of 0 with a DSM made every ring obstacle NaN, which rejected every cell.
  The ring check is now skipped when the ring has zero width.
* Approach rays that left the raster were silently truncated, so edge candidates looked clear.
  Coverage is now measured per ray and per sector. Sectors below the coverage threshold are
  `INCOMPLETE`, and candidates with no assessable sector are rated `Unassessed`.
* `GridGeometry.row_col` truncated with `int()`; it now uses `floor`.
* `max_upslope_landing_deg` was validated but never used. Slopes between the general and the
  upslope limit now pass as **upslope landing only**. They need an approach aligned within
  ±30° of the uphill landing heading, and their rating is capped at Suitable.
* DSM voids no longer invalidate otherwise good DTM cells. Obstacle coverage is tracked
  separately.
* Ring obstacle coverage is required: cells outside the raster count as unknown, not clear.
* `run_started_utc` is now recorded at the start of the run, not the end.

### Rating and readability

* Every candidate always receives a rating: Highly suitable, Suitable, Marginal, Unsuitable or
  Unassessed. Doctrine approval is reported in a separate **Status** field. Previously, every
  candidate under an unapproved profile was labelled `SCREENING_CANDIDATE`.
* Safety caps and gates:
  * No DSM: the rating is capped at Marginal.
  * Steep approach only: capped at Marginal.
  * All sectors blocked, no usable approach, or density altitude above the hover ceiling:
    Unsuitable.
* The approach score now combines two parts:
  * 80% from the best sector's obstacle angle;
  * 20% from approach breadth (the share of clear sectors, which reflects wind options).
* Candidates are ranked best first and given IDs `HLZ-01`, `HLZ-02` and so on.
* The limiting factor is plain language, for example
  `Best approach obstacle angle 4.2 deg (clear <= 3 deg)`.
* MGRS (1 m) and WGS84 latitude/longitude are added. Datum transformations are applied when
  needed.
* Map output:
  * Grouped, styled layers using traffic-light colours.
  * Labels on candidates.
  * Best-approach lines.
  * A cleared-ring outline layer.
  * A Ground_Class raster coloured green (any heading) or amber (upslope only).
  * Supporting layers hidden by default.
* New HTML report: rating cards, an overview map, a ranked table, a per-candidate approach rose,
  warnings and limitations.
* The CSV no longer starts with `#` comment lines, which broke Excel and ArcGIS import. Licence
  and provenance moved to `run_metadata.json`.
* Field aliases are human-readable, and messages in the Geoprocessing pane are clear.

### Performance and scale

* The O(N·r²) Numba kernel is replaced by FFT moment plane fitting, which costs the same for any
  window size, with row tiling and a halo of at least 2 × the TDP radius.
* Every final candidate gets an exact re-check (maximum plane residual and true-disk obstacles).
  When a check fails, the nearby cells are skipped so the exact-check budget lasts longer.
* Approach analysis is vectorised, and each ray is reused by every overlapping sector.
* The default cell limit rose from 250,000 to 10,000,000. An AOI polygon now limits where
  candidates are placed, while extra data around it is read for the approach checks.

### Toolbox UX

* Aircraft profile dropdown, with a custom-file option.
* Optional AOI polygon, Z units in metres or feet, and automatic alignment of a DSM on a
  different grid.
* Validation in the tool dialog: CRS and units, square cells, cell budget, AOI buffer coverage,
  DSM mismatch, missing DSM, and altimeter units.
* Each run writes to its own folder and never overwrites an earlier run. The HTML report opens
  automatically.
* New **2. Validate Profiles** tool.

### Tests

* 18 core unit tests. They need only NumPy and SciPy, so they run in the ArcGIS clone.
* A CLI end-to-end test with scene-based assertions:
  * the ditch is avoided;
  * no candidate is placed in the forest;
  * the no-DSM cap holds.
* An ArcPy smoke test that runs three scenarios through the real toolbox and writes a detailed
  log.
* A test-only fake `arcpy` (`tests/fake_arcpy`) for developer wiring checks without ArcGIS.
