# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""ArcGIS Pro Python toolbox for the HLZ Suitability Tool v0.10.3.

v0.10.3 runs entirely inside ArcGIS Pro's own Python (arcgispro-py3): the
engine needs only NumPy, SciPy and ArcPy, so there is no external Conda
environment, no subprocess and no extension licence to configure.

Usage: Catalog pane > Toolboxes > Add Toolbox > select this .pyt. Start with
"0. Set Up and Audit Data Folder" (creates HLZ_DATA and checks what is in it),
then open "1. Analyze HLZ Suitability" and choose that data folder. Keep the .pyt inside the release folder: it
loads ``hlzcore`` and ``profiles`` from its parent folder.
"""

from __future__ import annotations

import importlib
import logging
import math
import os
from dataclasses import replace
import re
import sys
from datetime import datetime
from pathlib import Path

import arcpy

RELEASE_ROOT = Path(__file__).resolve().parent.parent
if str(RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(RELEASE_ROOT))

# ArcGIS Pro caches imported modules for the whole session. Reload the engine
# so edits to hlzcore take effect after "Refresh" on the toolbox.
_MODULES = (
    "hlzcore",
    "hlzcore.models",
    "hlzcore.config",
    "hlzcore.geo",
    "hlzcore.search",
    "hlzcore.geometry_utils",
    "hlzcore.performance",
    "hlzcore.terrain",
    "hlzcore.masks",
    "hlzcore.approach",
    "hlzcore.candidates",
    "hlzcore.scoring",
    "hlzcore.report_map",
    "hlzcore.report",
    "hlzcore.tiling",
    "hlzcore.help_text",
    "hlzcore.data_root",
    "hlzcore.pipeline",
    "hlzcore.io_arcpy",
)
for _name in _MODULES:
    if _name in sys.modules:
        importlib.reload(sys.modules[_name])
    else:
        importlib.import_module(_name)

from hlzcore import __version__  # noqa: E402
from hlzcore.config import (  # noqa: E402
    override_aircraft,
    ProfileError,
    check_profile_compatibility,
    list_profiles,
    load_aircraft_profile,
    load_doctrine_profile,
    load_mission_profile,
    profiles_root,
)
from hlzcore.io_arcpy import (  # noqa: E402
    ArcpyWriter,
    add_results_to_map,
    dtm_problems,
    project_geographic_raster,
    latlon_to_xy,
    ArcpyTileReader,
    audit_inspector,
    open_file,
    raster_info,
    same_spatial_reference,
    view_extent_polygon,
)
from hlzcore.help_text import guidance, path_warning  # noqa: E402
from hlzcore import data_root as hlz_data  # noqa: E402
from hlzcore.search import parse_search_centre  # noqa: E402
from hlzcore.tiling import ResumeMismatch, checkpoint_status  # noqa: E402
from hlzcore.pipeline import (  # noqa: E402
    LOGGER,
    AnalysisError,
    RunOptions,
    close_logging,
    AnalysisCancelled,
    configure_logging,
    estimate_runtime_text,
    plan_for,
    run_tiled_pipeline,
)

CUSTOM_AIRCRAFT = "Custom profile file..."

# Parameter indices (keep in sync with getParameterInfo). v0.6.6 added the data folder as field 0, v0.6.7 the line width (field 25), v0.6.9 the obstacle fields (26-31), v0.7.0 the point radius and the overview grid (32-34), v0.8.0 the drawn area and the report units (35-36), v0.9.0 the size overrides, the minimum spacing and automatic projection (37-40).
P_DATA_ROOT = 0
P_DTM, P_DSM, P_AOI = 1, 2, 3
P_CENTRE_LAYER, P_CENTRE_TEXT, P_RADIUS = 4, 5, 6
P_USE_VIEW, P_EXCLUSION, P_ZUNITS = 7, 8, 9
P_AIRCRAFT, P_AIRCRAFT_FILE = 10, 11
P_OAT, P_ALTIMETER, P_HOVER = 12, 13, 14
P_OUT_FOLDER, P_RUN_NAME, P_ADD_MAP, P_OPEN_REPORT = 15, 16, 17, 18
P_MISSION, P_DOCTRINE, P_MAX_CANDIDATES, P_DIAGNOSTICS = 19, 20, 21, 22
P_BASEMAP, P_GUIDANCE = 23, 24
P_LINE_WIDTH = 25  # appended before the derived outputs so every earlier position is unchanged
P_OBST_LAYERS, P_OBST_FILES, P_OBST_FIELD, P_OBST_UNITS, P_OBST_DEFAULT, P_OBST_BUFFER = 26, 27, 28, 29, 30, 31  # v0.6.9
P_POINT_RADIUS, P_OVERVIEW, P_OVERVIEW_SIZE = 32, 33, 34  # v0.7.0
P_DRAWN_AOI, P_REPORT_UNITS = 35, 36  # v0.8.0: area drawn on the map, and the length units of the report
P_TDP_OVERRIDE, P_RING_OVERRIDE, P_MIN_SPACING, P_AUTO_PROJECT = 37, 38, 39, 40  # v0.9.0
P_OUT_FC, P_OUT_REPORT = 41, 42
OBSTACLE_FILE_SUFFIXES = (".dat", ".csv", ".txt", ".geojson", ".json")

# "0. Set Up and Audit Data Folder"
D_PARENT, D_NAME, D_CREATE, D_AIRCRAFT, D_OPEN, D_OUT_ROOT, D_OUT_REPORT = 0, 1, 2, 3, 4, 5, 6


class _ArcpyHandler(logging.Handler):
    """Forward engine log records to the geoprocessing message pane."""

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if record.levelno >= logging.ERROR:
            arcpy.AddError(message)
        elif record.levelno >= logging.WARNING:
            arcpy.AddWarning(message)
        elif record.levelno >= logging.INFO:
            arcpy.AddMessage(message)
            try:
                arcpy.SetProgressorLabel(message[:120])
            except Exception:  # noqa: BLE001
                pass


def _aircraft_choices() -> dict[str, Path]:
    try:
        return list_profiles("aircraft")
    except Exception:  # noqa: BLE001 - never break the dialog
        return {}


def _exclusion_layers(parameter) -> list:  # type: ignore[no-untyped-def]
    """The exclusion polygon layers chosen in the (multi-value) field, as strings."""
    if not parameter.valueAsText:
        return []
    values = getattr(parameter, "values", None)
    if values:
        return [str(value) for value in values]
    return [item.strip("'") for item in parameter.valueAsText.split(";") if item]


def _multi_values(parameter) -> list:  # type: ignore[no-untyped-def]
    """The items of a multi-value field as strings."""
    return _exclusion_layers(parameter)


def _obstacle_inputs(parameters):  # type: ignore[no-untyped-def]
    """(layers, files): GIS layers and FAA DOF / CSV / GeoJSON files chosen for the obstacle fields."""
    layers = _multi_values(parameters[P_OBST_LAYERS])
    files = _multi_values(parameters[P_OBST_FILES])
    return layers, files


def _has_centre(parameters) -> bool:  # type: ignore[no-untyped-def]
    return bool(parameters[P_CENTRE_LAYER].valueAsText or parameters[P_CENTRE_TEXT].valueAsText)


def _search_centres(parameters, dtm_sr):  # type: ignore[no-untyped-def]
    """Search centres in the DTM\u2019s coordinate system: the points of the layer, plus the typed position."""
    centres = []
    layer = parameters[P_CENTRE_LAYER].valueAsText
    if layer:
        with arcpy.da.SearchCursor(layer, ["SHAPE@XY"], spatial_reference=dtm_sr) as cursor:
            for (xy,) in cursor:
                if xy is not None and xy[0] is not None:
                    centres.append((float(xy[0]), float(xy[1])))
    text = parameters[P_CENTRE_TEXT].valueAsText
    if text:
        latitude, longitude, _ = parse_search_centre(text)
        centres.append(latlon_to_xy(latitude, longitude, dtm_sr))
    return centres


def _circle_extent(parameters, dtm_info):  # type: ignore[no-untyped-def]
    """Bounding box of the search circles, or None when no centre and radius are given."""
    radius = parameters[P_RADIUS].value
    if not radius or not _has_centre(parameters):
        return None
    try:
        centres = _search_centres(parameters, dtm_info["sr"])
    except Exception:  # noqa: BLE001 - the dialog reports bad input separately
        return None
    if not centres:
        return None
    r = float(radius)
    return arcpy.Extent(min(x for x, _ in centres) - r, min(y for _, y in centres) - r,
                        max(x for x, _ in centres) + r, max(y for _, y in centres) + r)


def _basemap_choice(parameter) -> str:  # type: ignore[no-untyped-def]
    return "OFFLINE" if (parameter.valueAsText or "").upper().startswith("NONE") else "ONLINE"


def _override_value(parameter):  # type: ignore[no-untyped-def]
    return None if parameter.value is None or parameter.valueAsText in (None, "") else float(parameter.value)


def _with_size_overrides(parameters, aircraft):  # type: ignore[no-untyped-def]
    """The aircraft profile with the optional landing-point diameter / cleared ring typed in the dialog (v0.9.0)."""
    return override_aircraft(aircraft, _override_value(parameters[P_TDP_OVERRIDE]), _override_value(parameters[P_RING_OVERRIDE]))


UNITS_CHOICES = {
    "Metres and feet": "BOTH",
    "Metres only": "METRIC",
    "Feet first (feet and metres)": "IMPERIAL",
}


def _units_choice(parameter) -> str:  # type: ignore[no-untyped-def]
    return UNITS_CHOICES.get(parameter.valueAsText or "", "BOTH")


def _drawn_feature_count(value) -> int:  # type: ignore[no-untyped-def]
    """Number of polygons in the area drawn on the map (0 when nothing was drawn)."""
    if value is None:
        return 0
    try:
        return int(str(arcpy.management.GetCount(value)[0]))
    except Exception:  # noqa: BLE001 - an empty or unreadable feature set counts as nothing drawn
        return 0


def _has_drawn_aoi(parameters) -> bool:  # type: ignore[no-untyped-def]
    return _drawn_feature_count(parameters[P_DRAWN_AOI].value) > 0


def _drawn_aoi_copy(parameters, workspace: str, name: str = "hlz_drawn_aoi"):  # type: ignore[no-untyped-def]
    """Copy the polygons drawn on the map into ``workspace`` and return the path (None when nothing was drawn)."""
    if not _has_drawn_aoi(parameters):
        return None
    out = os.path.join(workspace, name) if workspace != "memory" else f"memory\\{name}"
    if arcpy.Exists(out):
        arcpy.management.Delete(out)
    arcpy.management.CopyFeatures(parameters[P_DRAWN_AOI].value, out)
    return out


_DISCOVERY_CACHE: dict[str, tuple[float, "hlz_data.DiscoveredInputs"]] = {}


def _discover(root_text: str | None):  # type: ignore[no-untyped-def]
    """Inputs found in a data folder (pure-Python scan, cached for 5 s so the dialog stays fast)."""
    if not root_text:
        return None
    root = hlz_data.resolve_data_root(root_text) or Path(root_text)
    key = str(root)
    now = datetime.now().timestamp()
    cached = _DISCOVERY_CACHE.get(key)
    if cached and now - cached[0] < 5.0:
        return cached[1]
    found = hlz_data.discover_inputs(root)
    _DISCOVERY_CACHE[key] = (now, found)
    return found


def _fill_from_data_root(parameters, found) -> list[str]:  # type: ignore[no-untyped-def]
    """Fill EMPTY fields from a data folder. Returns what was filled (for messages)."""
    filled = []
    if found is None:
        return filled
    if not parameters[P_DTM].valueAsText and found.dtm:
        parameters[P_DTM].value = found.dtm
        filled.append(f"DTM = {found.dtm}")
    if not parameters[P_DSM].valueAsText and found.dsm:
        parameters[P_DSM].value = found.dsm
        filled.append(f"DSM = {found.dsm}")
    if not parameters[P_EXCLUSION].valueAsText and found.exclusions:
        parameters[P_EXCLUSION].value = ";".join(found.exclusions)
        filled.append(f"Exclusion areas = {len(found.exclusions)} layer(s)")
    if not parameters[P_OBST_LAYERS].valueAsText and not parameters[P_OBST_FILES].valueAsText and getattr(found, "obstacles", None):
        files = [path for path in found.obstacles if path.lower().endswith(OBSTACLE_FILE_SUFFIXES)]
        layers = [path for path in found.obstacles if not path.lower().endswith(OBSTACLE_FILE_SUFFIXES)]
        if files:
            parameters[P_OBST_FILES].value = ";".join(files)
        if layers:
            parameters[P_OBST_LAYERS].value = ";".join(layers)
        filled.append(f"Obstacles = {len(files) + len(layers)} source(s)")
    if not parameters[P_AOI].valueAsText and found.aoi and not _has_centre(parameters) and not _has_drawn_aoi(parameters):
        parameters[P_AOI].value = found.aoi
        filled.append(f"AOI = {found.aoi}")
    return filled


def _safe_name(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_\-]+", "_", text).strip("_")
    return cleaned[:60] or "HLZ_run"


def _aircraft_path(parameters) -> Path | None:  # type: ignore[no-untyped-def]
    choice = parameters[P_AIRCRAFT].valueAsText
    if choice == CUSTOM_AIRCRAFT:
        text = parameters[P_AIRCRAFT_FILE].valueAsText
        return Path(text) if text else None
    return _aircraft_choices().get(choice or "")


class Toolbox:
    """HLZ Suitability Tool."""

    def __init__(self):
        self.label = f"HLZ Suitability Tool {__version__}"
        self.alias = "hlz"
        self.tools = [PrepareDataFolder, AnalyzeHLZ, ValidateProfiles]


class PrepareDataFolder:
    """Create the HLZ data folder layout and audit what is in it."""

    def __init__(self):
        self.label = "0. Set Up and Audit Data Folder"
        self.description = (
            "Creates a standard data folder (HLZ_DATA) with a sub-folder for every input - DTM, DSM, hydrology, "
            "exclusions, AOI, search centres, profiles, runs - and a README in each. Then scans it and writes an "
            "audit report: what is missing, what is wrong and how to fix it, and which files the analysis will use. "
            "Nothing is ever moved or deleted."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        parent = arcpy.Parameter(
            displayName="Data folder location (the HLZ data folder is created inside it)",
            name="data_parent",
            datatype="DEFolder",
            parameterType="Required",
            direction="Input",
        )
        name = arcpy.Parameter(
            displayName="Data folder name",
            name="data_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        name.value = hlz_data.DEFAULT_ROOT_NAME
        create = arcpy.Parameter(
            displayName="Create missing folders (nothing is moved or deleted)",
            name="create_layout",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        create.value = True
        aircraft = arcpy.Parameter(
            displayName="Aircraft for the AOI margin check",
            name="audit_aircraft",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
        )
        choices = list(_aircraft_choices().keys())
        aircraft.filter.type = "ValueList"
        aircraft.filter.list = choices
        default = next((label for label in choices if "generic_medium" in label or "Generic medium" in label), None)
        if default:
            aircraft.value = default
        open_report = arcpy.Parameter(
            displayName="Open the audit report when finished",
            name="open_audit_report",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        open_report.value = True
        out_root = arcpy.Parameter(
            displayName="HLZ data folder (output)",
            name="out_data_root",
            datatype="DEFolder",
            parameterType="Derived",
            direction="Output",
        )
        out_report = arcpy.Parameter(
            displayName="Audit report (output)",
            name="out_audit_report",
            datatype="DEFile",
            parameterType="Derived",
            direction="Output",
        )
        return [parent, name, create, aircraft, open_report, out_root, out_report]

    def isLicensed(self):
        return True

    @staticmethod
    def _target(parameters) -> Path | None:  # type: ignore[no-untyped-def]
        parent = parameters[D_PARENT].valueAsText
        if not parent:
            return None
        if hlz_data.is_data_root(parent):
            return Path(parent)
        return Path(parent) / _safe_name(parameters[D_NAME].valueAsText or hlz_data.DEFAULT_ROOT_NAME)

    def updateParameters(self, parameters):
        text = parameters[D_NAME].valueAsText
        if text and _safe_name(text) != text:
            parameters[D_NAME].value = _safe_name(text)
        return

    def updateMessages(self, parameters):
        parent = parameters[D_PARENT].valueAsText
        target = self._target(parameters)
        if parent and hlz_data.is_data_root(parent):
            parameters[D_PARENT].setWarningMessage(
                "This folder is already an HLZ data folder: it will be audited (the name below is ignored)."
            )
        elif target is not None:
            message = path_warning(str(target))
            if message:
                parameters[D_PARENT].setWarningMessage(message.replace("C:\\GIS\\HLZ_runs", "C:\\GIS"))
            elif hlz_data.is_data_root(target):
                parameters[D_NAME].setWarningMessage(f"Existing data folder: {target}. It will be audited; missing "
                                                     "folders are added if the box below is ticked.")
            elif target.exists():
                parameters[D_NAME].setWarningMessage(f"{target} exists but is not a data folder yet; the layout is "
                                                     "added inside it. Existing files are kept and audited.")
            else:
                parameters[D_NAME].setWarningMessage(f"GUIDANCE: the data folder {target} will be created.")
        if target is not None and not parameters[D_CREATE].value and not target.exists():
            parameters[D_CREATE].setErrorMessage("The folder does not exist yet: tick this box to create it.")
        return

    def execute(self, parameters, messages):
        target = self._target(parameters)
        if target is None:
            raise arcpy.ExecuteError("Choose the data folder location.")
        aircraft_path = _aircraft_choices().get(parameters[D_AIRCRAFT].valueAsText or "")
        try:
            result = hlz_data.audit_data_root(
                target,
                create=bool(parameters[D_CREATE].value),
                aircraft_profile=aircraft_path,
                inspector=audit_inspector,
            )
        except (OSError, ProfileError) as error:
            arcpy.AddError(str(error))
            raise arcpy.ExecuteError(str(error)) from error
        counts = result.counts()
        arcpy.AddMessage("=" * 100)
        arcpy.AddMessage(f"HLZ data folder : {result.root}")
        if result.created_folders:
            arcpy.AddMessage("Created         : " + ", ".join(result.created_folders))
        arcpy.AddMessage(
            f"Findings        : {counts['FAIL']} FAIL, {counts['WARN']} WARN, {counts['INFO']} INFO, {counts['PASS']} PASS"
        )
        arcpy.AddMessage("=" * 100)
        for item in result.findings:
            text = f"[{item.level}] {hlz_data._area_title(item.area)}: {item.message}"
            if item.action:
                text += f"  -> {item.action}"
            if item.level in ("FAIL", "WARN"):
                arcpy.AddWarning(text)
            elif item.level == "INFO":
                arcpy.AddMessage(text)
        inputs = result.inputs
        arcpy.AddMessage(" ")
        arcpy.AddMessage("Inputs '1. Analyze HLZ Suitability' will use when you choose this data folder:")
        arcpy.AddMessage(f"  DTM             : {inputs.dtm or '-- none --'}")
        arcpy.AddMessage(f"  DSM             : {inputs.dsm or '-- none --'}")
        arcpy.AddMessage(f"  Exclusion areas : {len(inputs.exclusions)} layer(s)")
        arcpy.AddMessage(f"  AOI             : {inputs.aoi or '-- none (map view or whole DTM) --'}")
        arcpy.AddMessage(f"  Output folder   : {inputs.runs_folder or '-- not set --'}")
        arcpy.AddMessage(" ")
        arcpy.AddMessage(f"Audit report    : {result.reports.get('html', '')}")
        if result.status == hlz_data.STATUS_NOT_READY:
            arcpy.AddWarning("RESULT: " + hlz_data.STATUS_TEXT[result.status])
        else:
            arcpy.AddMessage("RESULT: " + hlz_data.STATUS_TEXT[result.status])
        arcpy.SetParameterAsText(D_OUT_ROOT, result.root)
        arcpy.SetParameterAsText(D_OUT_REPORT, result.reports.get("latest", ""))
        _DISCOVERY_CACHE.clear()
        if parameters[D_OPEN].value and result.reports.get("latest"):
            open_file(result.reports["latest"])
        return

    def postExecute(self, parameters):
        return


class AnalyzeHLZ:
    """Find and rank helicopter landing-zone candidates."""

    _last_area_km2: float | None = None

    def __init__(self):
        self.label = "1. Analyze HLZ Suitability"
        self.description = (
            "Screens a bare-earth DTM (and optional DSM) for helicopter landing-zone candidates: "
            "slope, surface roughness, obstacles in the touchdown point and cleared ring, approach "
            "and departure sectors, and optional density altitude. Results are ranked, coloured by "
            "rating and summarised in an HTML report with MGRS references. Planning aid only - "
            "confirm every site by reconnaissance."
        )
        self.category = ""
        self.canRunInBackground = False

    def getParameterInfo(self):
        data_root = arcpy.Parameter(
            displayName="HLZ data folder (optional) - fills the DTM, DSM, water/exclusion layers, AOI and output folder",
            name="data_root",
            datatype="DEFolder",
            parameterType="Optional",
            direction="Input",
        )
        dtm = arcpy.Parameter(
            displayName="Bare-earth elevation (DTM) - required unless the data folder holds one",
            name="in_dtm",
            datatype="GPRasterLayer",
            parameterType="Optional",
            direction="Input",
        )
        dsm = arcpy.Parameter(
            displayName="Surface model with trees/buildings (DSM) - strongly recommended",
            name="in_dsm",
            datatype="GPRasterLayer",
            parameterType="Optional",
            direction="Input",
        )
        aoi = arcpy.Parameter(
            displayName="Area of interest (polygon) - only places LZ centres inside it",
            name="in_aoi",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input",
        )
        aoi.filter.list = ["Polygon"]
        centre_layer = arcpy.Parameter(
            displayName="Search around a point: centre point layer (optional, needs a radius)",
            name="in_centre",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input",
        )
        centre_layer.filter.list = ["Point"]
        centre_text = arcpy.Parameter(
            displayName="Search around a point: centre typed as MGRS or latitude, longitude (optional)",
            name="centre_text",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
        )
        radius = arcpy.Parameter(
            displayName="Search around a point: radius (m)",
            name="search_radius_m",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
        )
        radius.filter.type = "Range"
        radius.filter.list = [1, 5000000]
        use_view = arcpy.Parameter(
            displayName="If no AOI is given, use the current map view extent",
            name="use_map_view",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        use_view.value = True
        exclusion = arcpy.Parameter(
            displayName="Exclusion areas: water, wetlands, known hazards, ... (polygons, lines or points a landing zone must not touch)",
            name="in_exclusion",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input",
            multiValue=True,
        )
        exclusion.filter.list = ["Polygon", "Polyline", "Point", "Multipoint"]
        z_units = arcpy.Parameter(
            displayName="Elevation (Z) units",
            name="z_units",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        z_units.filter.type = "ValueList"
        z_units.filter.list = ["Meters", "Feet"]
        z_units.value = "Meters"

        aircraft = arcpy.Parameter(
            displayName="Aircraft profile",
            name="aircraft",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        choices = list(_aircraft_choices())
        aircraft.filter.type = "ValueList"
        aircraft.filter.list = choices + [CUSTOM_AIRCRAFT]
        default_choice = next((label for label in choices if "generic_medium" in label), None)
        aircraft.value = default_choice or (choices[0] if choices else CUSTOM_AIRCRAFT)
        aircraft_file = arcpy.Parameter(
            displayName="Custom aircraft profile (.json)",
            name="aircraft_file",
            datatype="DEFile",
            parameterType="Optional",
            direction="Input",
        )
        aircraft_file.filter.list = ["json", "yaml", "yml"]
        aircraft_file.enabled = False

        oat = arcpy.Parameter(
            displayName="Outside air temperature (C)",
            name="oat_c",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Weather (optional)",
        )
        altimeter = arcpy.Parameter(
            displayName="Altimeter setting (inHg)",
            name="altimeter_inhg",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Weather (optional)",
        )
        altimeter.value = 29.92
        hover = arcpy.Parameter(
            displayName="Hover performance mode",
            name="hover_mode",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
            category="Weather (optional)",
        )
        hover.filter.type = "ValueList"
        hover.filter.list = ["OGE", "IGE"]
        hover.value = "OGE"

        out_folder = arcpy.Parameter(
            displayName="Output folder",
            name="out_folder",
            datatype="DEFolder",
            parameterType="Required",
            direction="Input",
        )
        try:
            out_folder.value = arcpy.mp.ArcGISProject("CURRENT").homeFolder
        except Exception:  # noqa: BLE001
            pass
        run_name = arcpy.Parameter(
            displayName="Run name (sub-folder created in the output folder)",
            name="run_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        run_name.value = f"HLZ_{datetime.now():%Y%m%d_%H%M}"
        add_map = arcpy.Parameter(
            displayName="Add styled results to the current map",
            name="add_to_map",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        add_map.value = True
        open_report = arcpy.Parameter(
            displayName="Open the HTML report when finished",
            name="open_report",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        open_report.value = True

        mission = arcpy.Parameter(
            displayName="Mission profile (.json)",
            name="mission_file",
            datatype="DEFile",
            parameterType="Required",
            direction="Input",
            category="Advanced",
        )
        mission.filter.list = ["json", "yaml", "yml"]
        mission.value = str(profiles_root() / "missions" / "screening.json")
        doctrine = arcpy.Parameter(
            displayName="Doctrine / threshold profile (.json)",
            name="doctrine_file",
            datatype="DEFile",
            parameterType="Required",
            direction="Input",
            category="Advanced",
        )
        doctrine.filter.list = ["json", "yaml", "yml"]
        doctrine.value = str(profiles_root() / "doctrine" / "generic_planning.json")
        max_candidates = arcpy.Parameter(
            displayName="Maximum number of candidates (blank = mission profile)",
            name="max_candidates",
            datatype="GPLong",
            parameterType="Optional",
            direction="Input",
            category="Advanced",
        )
        max_candidates.filter.type = "Range"
        max_candidates.filter.list = [1, 100000]
        diagnostics = arcpy.Parameter(
            displayName="Slope / roughness / obstacle rasters",
            name="diagnostic_rasters",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            category="Advanced",
        )
        diagnostics.filter.type = "ValueList"
        diagnostics.filter.list = ["AUTO", "ALWAYS", "NEVER"]
        diagnostics.value = "AUTO"
        basemap = arcpy.Parameter(
            displayName="Basemap in the HTML report",
            name="report_basemap",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            category="Advanced",
        )
        basemap.filter.type = "ValueList"
        basemap.filter.list = ["Online imagery and topographic map", "None (offline, no data leaves this PC)"]
        basemap.value = "Online imagery and topographic map"
        show_guidance = arcpy.Parameter(
            displayName="Explain each field in the dialog (guidance)",
            name="show_guidance",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        show_guidance.value = True

        line_width = arcpy.Parameter(
            displayName="Width given to exclusion LINES such as rivers and streams (m)",
            name="line_width_m",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
        )
        line_width.value = 10.0
        line_width.filter.type = "Range"
        line_width.filter.list = [0, 500]

        obst_layers = arcpy.Parameter(
            displayName="Obstacles: towers, masts, wires, buildings - layers (points, lines or polygons with a height field)",
            name="in_obstacle_layers",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input",
            multiValue=True,
        )
        obst_layers.filter.list = ["Point", "Multipoint", "Polyline", "Polygon"]
        obst_files = arcpy.Parameter(
            displayName="Obstacles: FAA Digital Obstacle File (.dat), CSV or GeoJSON files",
            name="in_obstacle_files",
            datatype="DEFile",
            parameterType="Optional",
            direction="Input",
            multiValue=True,
        )
        obst_files.filter.list = ["dat", "csv", "txt", "geojson", "json"]
        obst_field = arcpy.Parameter(
            displayName="Obstacle height field (leave empty to find it automatically)",
            name="obstacle_height_field",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            category="Obstacle settings",
        )
        obst_units = arcpy.Parameter(
            displayName="Obstacle height units (FAA DOF is always feet)",
            name="obstacle_height_units",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            category="Obstacle settings",
        )
        obst_units.filter.type = "ValueList"
        obst_units.filter.list = ["Meters", "Feet"]
        obst_units.value = "Meters"
        obst_default = arcpy.Parameter(
            displayName="Height (m) used for obstacles that have none",
            name="obstacle_default_height_m",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Obstacle settings",
        )
        obst_default.value = 30.0
        obst_default.filter.type = "Range"
        obst_default.filter.list = [1, 500]
        obst_buffer = arcpy.Parameter(
            displayName="Horizontal buffer around each obstacle (m)",
            name="obstacle_buffer_m",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Obstacle settings",
        )
        obst_buffer.value = 10.0
        obst_buffer.filter.type = "Range"
        obst_buffer.filter.list = [0, 200]

        point_radius = arcpy.Parameter(
            displayName="Radius given to exclusion POINTS such as a crane, pole or known hazard (m)",
            name="point_radius_m",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
        )
        point_radius.value = 10.0
        point_radius.filter.type = "Range"
        point_radius.filter.list = [0, 500]

        build_overview = arcpy.Parameter(
            displayName="Also create a terrain overview grid (coarse cells showing where the ground passes)",
            name="build_overview",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
            category="Overview grid",
        )
        build_overview.value = False
        overview_size = arcpy.Parameter(
            displayName="Overview grid cell size (m)",
            name="overview_cell_m",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Overview grid",
        )
        overview_size.value = 500.0
        overview_size.filter.type = "Range"
        overview_size.filter.list = [20, 20000]

        drawn_aoi = arcpy.Parameter(
            displayName="Or draw the area of interest on the map (polygon)",
            name="drawn_aoi",
            datatype="GPFeatureRecordSetLayer",
            parameterType="Optional",
            direction="Input",
        )
        try:  # the template gives the feature set its polygon schema, so the Pro drawing tools appear
            arcpy.management.CreateFeatureclass("memory", "HLZ_Draw_AOI", "POLYGON")
            drawn_aoi.value = "memory\\HLZ_Draw_AOI"
        except Exception:  # noqa: BLE001 - without it the field is just not drawable; the polygon layer field still works
            pass
        report_units = arcpy.Parameter(
            displayName="Length units in the HTML report",
            name="report_units",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            category="Advanced",
        )
        report_units.filter.type = "ValueList"
        report_units.filter.list = list(UNITS_CHOICES)
        report_units.value = "Metres and feet"

        tdp_override = arcpy.Parameter(
            displayName="What-if: landing-point diameter (m), replaces the aircraft profile's value",
            name="tdp_diameter_override_m",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Aircraft size (what-if)",
        )
        tdp_override.filter.type = "Range"
        tdp_override.filter.list = [5, 200]
        ring_override = arcpy.Parameter(
            displayName="What-if: cleared ring width (m), replaces the aircraft profile's value",
            name="cleared_ring_override_m",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Aircraft size (what-if)",
        )
        ring_override.filter.type = "Range"
        ring_override.filter.list = [0, 200]
        min_spacing = arcpy.Parameter(
            displayName="Minimum spacing between listed candidates (m) - 0 lists every candidate",
            name="min_spacing_m",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Advanced",
        )
        min_spacing.value = 0.0
        min_spacing.filter.type = "Range"
        min_spacing.filter.list = [0, 5000]
        auto_project = arcpy.Parameter(
            displayName="Project a latitude/longitude DTM or DSM to UTM automatically",
            name="auto_project",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
            category="Advanced",
        )
        auto_project.value = True

        out_fc = arcpy.Parameter(
            displayName="HLZ candidates (output)",
            name="out_candidates",
            datatype="DEFeatureClass",
            parameterType="Derived",
            direction="Output",
        )
        out_report = arcpy.Parameter(
            displayName="HTML report (output)",
            name="out_report",
            datatype="DEFile",
            parameterType="Derived",
            direction="Output",
        )
        return [
            data_root,
            dtm,
            dsm,
            aoi,
            centre_layer,
            centre_text,
            radius,
            use_view,
            exclusion,
            z_units,
            aircraft,
            aircraft_file,
            oat,
            altimeter,
            hover,
            out_folder,
            run_name,
            add_map,
            open_report,
            mission,
            doctrine,
            max_candidates,
            diagnostics,
            basemap,
            show_guidance,
            line_width,
            obst_layers,
            obst_files,
            obst_field,
            obst_units,
            obst_default,
            obst_buffer,
            point_radius,
            build_overview,
            overview_size,
            drawn_aoi,
            report_units,
            tdp_override,
            ring_override,
            min_spacing,
            auto_project,
            out_fc,
            out_report,
        ]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        root_param = parameters[P_DATA_ROOT]
        if root_param.valueAsText and root_param.altered and not root_param.hasBeenValidated:
            try:
                found = _discover(root_param.valueAsText)
                _fill_from_data_root(parameters, found)
                if found is not None and found.runs_folder and not parameters[P_OUT_FOLDER].altered:
                    parameters[P_OUT_FOLDER].value = found.runs_folder
            except Exception:  # noqa: BLE001 - never break the dialog
                pass
        custom = parameters[P_AIRCRAFT].valueAsText == CUSTOM_AIRCRAFT
        parameters[P_AIRCRAFT_FILE].enabled = custom
        parameters[P_OVERVIEW_SIZE].enabled = bool(parameters[P_OVERVIEW].value)
        if parameters[P_AIRCRAFT].altered and not parameters[P_RUN_NAME].altered:
            choice = parameters[P_AIRCRAFT].valueAsText or ""
            match = re.search(r"\[(.+?)\]", choice)
            stem = match.group(1) if match else "custom"
            parameters[P_RUN_NAME].value = f"HLZ_{stem}_{datetime.now():%Y%m%d_%H%M}"
        if parameters[P_RUN_NAME].valueAsText:
            safe = _safe_name(parameters[P_RUN_NAME].valueAsText)
            if safe != parameters[P_RUN_NAME].valueAsText:
                parameters[P_RUN_NAME].value = safe
        return

    def updateMessages(self, parameters):
        # Profiles ------------------------------------------------------------
        aircraft = None
        mission = None
        aircraft_path = _aircraft_path(parameters)
        if parameters[P_AIRCRAFT].valueAsText == CUSTOM_AIRCRAFT and not parameters[P_AIRCRAFT_FILE].valueAsText:
            parameters[P_AIRCRAFT_FILE].setErrorMessage("Select a custom aircraft profile file.")
        elif aircraft_path is not None:
            try:
                aircraft = load_aircraft_profile(aircraft_path)
            except ProfileError as error:
                target = P_AIRCRAFT_FILE if parameters[P_AIRCRAFT_FILE].enabled else P_AIRCRAFT
                parameters[target].setErrorMessage(str(error))
            if aircraft is not None:
                try:
                    aircraft = _with_size_overrides(parameters, aircraft)
                except ProfileError as error:
                    parameters[P_TDP_OVERRIDE if "diameter" in str(error) else P_RING_OVERRIDE].setErrorMessage(str(error))
                    aircraft = load_aircraft_profile(aircraft_path)
        if parameters[P_MISSION].valueAsText:
            try:
                mission = load_mission_profile(parameters[P_MISSION].valueAsText)
            except ProfileError as error:
                parameters[P_MISSION].setErrorMessage(str(error))
        if parameters[P_DOCTRINE].valueAsText:
            try:
                doctrine = load_doctrine_profile(parameters[P_DOCTRINE].valueAsText)
                if not doctrine.is_approved:
                    parameters[P_DOCTRINE].setWarningMessage(
                        "This threshold profile is not approved doctrine; results are labelled "
                        "'Planning only'."
                    )
            except ProfileError as error:
                parameters[P_DOCTRINE].setErrorMessage(str(error))
        if aircraft is not None and mission is not None:
            try:
                check_profile_compatibility(aircraft, mission)
            except ProfileError as error:
                parameters[P_MISSION].setErrorMessage(str(error))

        # Data folder ---------------------------------------------------------
        data_summary = None
        root_text = parameters[P_DATA_ROOT].valueAsText
        found = None
        if root_text:
            if hlz_data.resolve_data_root(root_text) is None:
                parameters[P_DATA_ROOT].setWarningMessage(
                    "This is not an HLZ data folder (no hlz_data_root.json). Create one with '0. Set Up and Audit "
                    "Data Folder', or choose the HLZ_DATA folder itself."
                )
            else:
                try:
                    found = _discover(root_text)
                except Exception as error:  # noqa: BLE001
                    parameters[P_DATA_ROOT].setWarningMessage(f"The data folder could not be scanned: {error}")
            if found is not None:
                parts = [
                    f"DTM: {Path(found.dtm).name if found.dtm else 'NONE'}",
                    f"DSM: {Path(found.dsm).name if found.dsm else 'none'}",
                    f"exclusion layers: {len(found.exclusions)}",
                    f"AOI: {Path(found.aoi).name if found.aoi else 'none'}",
                ]
                if found.centres:
                    parts.append(f"search-centre layers: {len(found.centres)} (choose one below if wanted)")
                data_summary = "Data folder: " + "; ".join(parts) + ". Empty fields are filled from it. " + " ".join(found.notes)
                if found.dtm is None:
                    parameters[P_DATA_ROOT].setWarningMessage(
                        data_summary + " No single DTM in 01_Elevation\\DTM: run '0. Set Up and Audit Data Folder' "
                        "to see why."
                    )
                elif not found.exclusions:
                    parameters[P_DATA_ROOT].setWarningMessage(
                        data_summary + " No water polygons in 02_Hydrology: lakes are NOT excluded."
                    )
        if not parameters[P_DTM].valueAsText and not (found is not None and found.dtm):
            parameters[P_DTM].setErrorMessage(
                "Choose a bare-earth DTM, or an HLZ data folder (first field) that holds one in 01_Elevation\\DTM."
            )

        # Area of interest: a polygon layer OR an area drawn on the map -------------
        if parameters[P_AOI].valueAsText and _has_drawn_aoi(parameters):
            parameters[P_DRAWN_AOI].setErrorMessage(
                "Use either the polygon layer or the area drawn here, not both. Clear one of them."
            )

        # DTM -----------------------------------------------------------------
        too_large = False
        dtm_info = None
        if parameters[P_DTM].valueAsText:
            try:
                dtm_info = raster_info(parameters[P_DTM].valueAsText)
            except Exception as error:  # noqa: BLE001
                parameters[P_DTM].setErrorMessage(f"Cannot read the DTM: {error}")
        if dtm_info is not None:
            auto_project = bool(parameters[P_AUTO_PROJECT].value)
            problems = dtm_problems(dtm_info, allow_geographic=auto_project)
            errors = [message for level, message in problems if level == "error"]
            warnings = [message for level, message in problems if level == "warning"]
            if aircraft is not None and dtm_info["sr_type"] != "Geographic" and dtm_info["cell_w"] > aircraft.tdp_radius_m / 2.0:
                warnings.append(
                    f"Cell size {dtm_info['cell_w']:.1f} m is coarse for a {aircraft.tdp_diameter_m:.0f} m "
                    "touchdown point."
                )
            if errors:
                parameters[P_DTM].setErrorMessage(" ".join(errors))
            else:
                if aircraft is not None and mission is not None and dtm_info["sr_type"] != "Geographic":
                    try:
                        too_large = self._check_budget(parameters, dtm_info, aircraft, mission)
                    except Exception as error:  # noqa: BLE001
                        parameters[P_AOI].setErrorMessage(str(error))
                if warnings and not too_large:
                    parameters[P_DTM].setWarningMessage(" ".join(warnings))

        # DSM -----------------------------------------------------------------
        if dtm_info is not None and parameters[P_DSM].valueAsText:
            try:
                dsm_info = raster_info(parameters[P_DSM].valueAsText)
                if dsm_info["band_count"] != 1:
                    parameters[P_DSM].setErrorMessage("The DSM must be a single-band raster.")
                elif not same_spatial_reference(dsm_info["sr"], dtm_info["sr"]):
                    parameters[P_DSM].setWarningMessage(
                        "The DSM coordinate system differs from the DTM; it will be projected onto the DTM grid."
                    )
                elif abs(dsm_info["cell_w"] - dtm_info["cell_w"]) > 1.0e-6:
                    parameters[P_DSM].setWarningMessage(
                        "The DSM cell size differs from the DTM; it will be resampled onto the DTM grid."
                    )
            except Exception as error:  # noqa: BLE001
                parameters[P_DSM].setErrorMessage(f"Cannot read the DSM: {error}")
        elif dtm_info is not None and not parameters[P_DSM].valueAsText:
            parameters[P_DSM].setWarningMessage(
                "Without a DSM, trees and buildings are not assessed and ratings are capped at Marginal."
            )

        # Weather -------------------------------------------------------------
        altimeter = parameters[P_ALTIMETER].value
        if altimeter is not None and not 25.0 <= float(altimeter) <= 32.5:
            parameters[P_ALTIMETER].setErrorMessage(
                "Enter the altimeter in inHg (25.0-32.5). Convert hPa: hPa / 33.864."
            )
        oat = parameters[P_OAT].value
        if oat is not None and not -70.0 <= float(oat) <= 60.0:
            parameters[P_OAT].setErrorMessage("Outside air temperature must be between -70 and +60 C.")

        # Output --------------------------------------------------------------
        folder = parameters[P_OUT_FOLDER].valueAsText
        name = parameters[P_RUN_NAME].valueAsText
        if folder and name and (Path(folder) / name).exists():
            if checkpoint_status(Path(folder) / name) == "incomplete":
                parameters[P_RUN_NAME].setWarningMessage(
                    "An unfinished run with this name exists: it will be RESUMED from the last finished tile "
                    "(use another name to start over)."
                )
            else:
                parameters[P_RUN_NAME].setWarningMessage(
                    "This run folder already exists; a numbered suffix will be added to keep earlier results."
                )
        typed_centre = parameters[P_CENTRE_TEXT].valueAsText
        if typed_centre:
            try:
                parse_search_centre(typed_centre)
            except ValueError as error:
                parameters[P_CENTRE_TEXT].setErrorMessage(str(error))
        radius_value = parameters[P_RADIUS].value
        if _has_centre(parameters) and not radius_value:
            parameters[P_RADIUS].setErrorMessage("Enter the search radius in metres (for example 2000) for the centre point.")
        elif radius_value and not _has_centre(parameters):
            parameters[P_RADIUS].setWarningMessage(
                "A radius needs a centre: choose a point layer or type an MGRS or latitude, longitude. Without a centre the radius is ignored."
            )

        exclusion_layers = _exclusion_layers(parameters[P_EXCLUSION])
        line_layers: list[str] = []
        point_layers: list[str] = []
        for layer in exclusion_layers:
            try:
                description = arcpy.Describe(layer)
                shape_type = getattr(description, "shapeType", "")
                if shape_type not in ("Polygon", "Polyline", "Point", "Multipoint"):
                    parameters[P_EXCLUSION].setErrorMessage(
                        f"'{layer}' holds {shape_type or 'unknown'} features. Exclusion areas must be polygons (lakes, "
                        "wetlands), lines (rivers, streams, shorelines) or points (known hazards)."
                    )
                    break
                if shape_type == "Polyline":
                    line_layers.append(layer)
                if shape_type in ("Point", "Multipoint"):
                    point_layers.append(layer)
                if dtm_info is not None:
                    box = description.extent
                    if not same_spatial_reference(description.spatialReference, dtm_info["sr"]):
                        box = box.projectAs(dtm_info["sr"])
                    dtm_box = dtm_info["extent"]
                    if box.XMax < dtm_box.XMin or box.XMin > dtm_box.XMax or box.YMax < dtm_box.YMin or box.YMin > dtm_box.YMax:
                        parameters[P_EXCLUSION].setWarningMessage(
                            f"'{layer}' does not overlap the DTM, so it will exclude nothing."
                        )
            except Exception:  # noqa: BLE001 - never break the dialog
                pass

        if line_layers:
            width = parameters[P_LINE_WIDTH].value
            if not parameters[P_LINE_WIDTH].hasError():
                parameters[P_LINE_WIDTH].setWarningMessage(
                    f"GUIDANCE: {len(line_layers)} line layer(s) are used as exclusion areas. A line has no width, so "
                    f"every line is widened to {10.0 if width is None else float(width):g} m in total (half each side). "
                    "Rivers wider than that should come as water POLYGONS. A closed lake outline excludes only the strip "
                    "along the shore, not the lake."
                )

        if point_layers and not parameters[P_POINT_RADIUS].hasError():
            radius = parameters[P_POINT_RADIUS].value
            parameters[P_POINT_RADIUS].setWarningMessage(
                f"GUIDANCE: {len(point_layers)} point layer(s) are used as exclusion areas. A point has no size, so every "
                f"point excludes a circle of {10.0 if radius is None else float(radius):g} m radius around it; the cleared-ring "
                "distance is added on top. Large hazards should be given as polygons."
            )

        obstacle_layers, obstacle_files = _obstacle_inputs(parameters)
        field_name = (parameters[P_OBST_FIELD].valueAsText or "").strip()
        for layer in obstacle_layers:
            try:
                description = arcpy.Describe(layer)
                shape_type = getattr(description, "shapeType", "")
                if shape_type not in ("Point", "Multipoint", "Polyline", "Polygon"):
                    parameters[P_OBST_LAYERS].setErrorMessage(
                        f"'{layer}' holds {shape_type or 'unknown'} features. Obstacles must be points, lines or polygons."
                    )
                    break
                if field_name and field_name.lower() not in [f.name.lower() for f in arcpy.ListFields(layer)]:
                    parameters[P_OBST_FIELD].setWarningMessage(
                        f"'{layer}' has no field '{field_name}'. Clear this field to find the height field automatically."
                    )
                if dtm_info is not None:
                    box = description.extent
                    if not same_spatial_reference(description.spatialReference, dtm_info["sr"]):
                        box = box.projectAs(dtm_info["sr"])
                    dtm_box = dtm_info["extent"]
                    if box.XMax < dtm_box.XMin or box.XMin > dtm_box.XMax or box.YMax < dtm_box.YMin or box.YMin > dtm_box.YMax:
                        parameters[P_OBST_LAYERS].setWarningMessage(f"'{layer}' does not overlap the DTM, so it adds no obstacle.")
            except Exception:  # noqa: BLE001 - never break the dialog
                pass
        for item in obstacle_files:
            if not item.lower().endswith(OBSTACLE_FILE_SUFFIXES):
                parameters[P_OBST_FILES].setErrorMessage(
                    f"'{item}' is not a supported obstacle file. Use an FAA DOF (.dat), a CSV (.csv) or GeoJSON (.geojson). "
                    "Shapefiles, GeoPackages and geodatabase layers go in the obstacle LAYERS field."
                )
                break
            if not os.path.isfile(item):
                parameters[P_OBST_FILES].setErrorMessage(f"The file '{item}' does not exist.")
                break

        path_message = path_warning(folder)
        if path_message:
            parameters[P_OUT_FOLDER].setWarningMessage(path_message)

        # Smart checks -----------------------------------------------------
        if dtm_info is not None and parameters[P_DSM].valueAsText:
            try:
                same = raster_info(parameters[P_DSM].valueAsText)["catalog_path"] == dtm_info["catalog_path"]
                if same:
                    parameters[P_DSM].setErrorMessage(
                        "The DSM is the same raster as the DTM, so no obstacles can be detected. "
                        "Select the surface model (top of trees/buildings) instead."
                    )
            except Exception:  # noqa: BLE001
                pass
        if dtm_info is not None and (parameters[P_ZUNITS].valueAsText or "Meters") == "Meters":
            try:
                vcs = getattr(dtm_info["sr"], "VCS", None)
                vertical_unit = (getattr(vcs, "linearUnitName", "") or "").lower() if vcs else ""
                if "foot" in vertical_unit or "feet" in vertical_unit:
                    parameters[P_ZUNITS].setWarningMessage(
                        f"The DTM's vertical coordinate system is in {vcs.linearUnitName}. Choose 'Feet' "
                        "unless the values were converted to metres."
                    )
            except Exception:  # noqa: BLE001
                pass

        # Field guidance (context-aware explanations) ------------------------
        if parameters[P_GUIDANCE].value is not False:
            context = {
                "dsm_given": bool(parameters[P_DSM].valueAsText),
                "aoi_given": bool(parameters[P_AOI].valueAsText),
                "drawn_given": _has_drawn_aoi(parameters),
                "units_choice": _units_choice(parameters[P_REPORT_UNITS]),
                "tdp_override": _override_value(parameters[P_TDP_OVERRIDE]),
                "ring_override": _override_value(parameters[P_RING_OVERRIDE]),
                "ring_effective": (
                    _override_value(parameters[P_RING_OVERRIDE]) if _override_value(parameters[P_RING_OVERRIDE]) is not None
                    else (aircraft.cleared_ring_m if aircraft is not None else None)
                ),
                "min_spacing": parameters[P_MIN_SPACING].value,
                "auto_project": bool(parameters[P_AUTO_PROJECT].value),
                "dtm_geographic": bool(dtm_info is not None and dtm_info.get("sr_type") == "Geographic"),
                "use_view": bool(parameters[P_USE_VIEW].value),
                "oat_given": parameters[P_OAT].value is not None,
                "has_ceilings": bool(
                    aircraft is not None
                    and (aircraft.hover_ceiling_oge_ft is not None or aircraft.hover_ceiling_ige_ft is not None)
                ),
                "altimeter": parameters[P_ALTIMETER].value,
                "profile_custom": parameters[P_AIRCRAFT].valueAsText == CUSTOM_AIRCRAFT,
                "area_km2": AnalyzeHLZ._last_area_km2,
                "diagnostics": parameters[P_DIAGNOSTICS].valueAsText,
                "exclusion_given": bool(exclusion_layers),
                "centre_given": _has_centre(parameters),
                "radius_given": bool(parameters[P_RADIUS].value),
                "basemap": _basemap_choice(parameters[P_BASEMAP]),
                "data_summary": data_summary,
                "lines_given": bool(line_layers),
                "points_given": bool(point_layers),
                "overview_on": bool(parameters[P_OVERVIEW].value),
                "obstacles_given": bool(obstacle_layers or obstacle_files),
                "obstacle_layers_given": bool(obstacle_layers),
                "obstacle_files_given": bool(obstacle_files),
                "obstacle_default_m": parameters[P_OBST_DEFAULT].value,
                "aircraft_sizes": (
                    (aircraft.display_name, aircraft.tdp_diameter_m, aircraft.cleared_ring_m)
                    if aircraft is not None else None
                ),
            }
            for parameter in parameters:
                if parameter.direction != "Input" or parameter.hasError() or parameter.hasWarning():
                    continue
                if not parameter.enabled:
                    continue
                text = guidance(parameter.name, context)
                if text:
                    parameter.setWarningMessage("GUIDANCE: " + text)
        return

    @staticmethod
    def _check_budget(parameters, dtm_info, aircraft, mission):  # type: ignore[no-untyped-def]
        """There is no area limit since v0.6.1; report the area, tiles and run time.

        Returns True only when the area cannot be analysed (no overlap).
        """
        cell = dtm_info["cell_w"]
        extent = dtm_info["extent"]
        aoi_extent = None
        source = None
        drawn_path = None if parameters[P_AOI].valueAsText else _drawn_aoi_copy(parameters, "memory")
        if parameters[P_AOI].valueAsText or drawn_path:
            description = arcpy.Describe(parameters[P_AOI].valueAsText or drawn_path)
            aoi_extent = description.extent
            if not same_spatial_reference(description.spatialReference, dtm_info["sr"]):
                aoi_extent = aoi_extent.projectAs(dtm_info["sr"])
            source = "AOI" if parameters[P_AOI].valueAsText else "drawn area"
        elif parameters[P_USE_VIEW].value and not _has_centre(parameters):
            polygon = view_extent_polygon(dtm_info["sr"])
            if polygon is not None:
                aoi_extent = polygon.extent
                source = "map view"
        circle_extent = _circle_extent(parameters, dtm_info)
        if circle_extent is not None:
            if aoi_extent is not None and source in ("AOI", "drawn area"):
                aoi_extent = arcpy.Extent(max(aoi_extent.XMin, circle_extent.XMin), max(aoi_extent.YMin, circle_extent.YMin),
                                          min(aoi_extent.XMax, circle_extent.XMax), min(aoi_extent.YMax, circle_extent.YMax))
                source = f"{source} and search circle"
            else:
                aoi_extent = circle_extent
                source = "search circle"
        if aoi_extent is None:
            x_min, y_min, x_max, y_max = extent.XMin, extent.YMin, extent.XMax, extent.YMax
        else:
            x_min = max(aoi_extent.XMin, extent.XMin)
            y_min = max(aoi_extent.YMin, extent.YMin)
            x_max = min(aoi_extent.XMax, extent.XMax)
            y_max = min(aoi_extent.YMax, extent.YMax)
            if x_max <= x_min or y_max <= y_min:
                parameters[P_DRAWN_AOI if source.startswith("drawn") else P_AOI].setErrorMessage(f"The {source} does not overlap the DTM.")
                return True
        cols = int((x_max - x_min) / cell)
        rows = int((y_max - y_min) / cell)
        tiles, _, _ = plan_for((0, rows, 0, cols), cell, aircraft, mission)
        area_km2 = (x_max - x_min) * (y_max - y_min) / 1.0e6
        what = {
            None: "Whole DTM", "AOI": "AOI", "map view": "Current map view (no AOI drawn)",
            "search circle": "Search circle(s)", "AOI and search circle": "Overlap of the AOI and the search circle(s)",
            "drawn area": "Area drawn on the map", "drawn area and search circle": "Overlap of the drawn area and the search circle(s)",
        }[source]
        cell_count = rows * cols
        if source and "circle" in source:
            circle_area = len(_search_centres(parameters, dtm_info["sr"])) * math.pi * float(parameters[P_RADIUS].value) ** 2
            area_km2 = min(area_km2 * 1.0e6, circle_area) / 1.0e6
            cell_count = int(area_km2 * 1.0e6 / (cell * cell))

        message = (
            f"{what}: {area_km2:,.1f} km2, {cell_count:,} cells -> "
            f"{estimate_runtime_text(cell_count, len(tiles), 'arcpy')}."
        )
        if len(tiles) > 1:
            message += " Interrupted runs resume when re-run with the same run name."
        if aoi_extent is not None:
            buffer_m = aircraft.approach_range_m + aircraft.analysis_radius_m
            gaps = {
                "west": aoi_extent.XMin - extent.XMin, "east": extent.XMax - aoi_extent.XMax,
                "south": aoi_extent.YMin - extent.YMin, "north": extent.YMax - aoi_extent.YMax,
            }
            short = [side for side, gap in gaps.items() if gap < buffer_m - 1.0]
            if short:
                message += (
                    f" The DTM ends less than {buffer_m:,.0f} m (the approach range plus the touchdown point and ring) "
                    f"from this area on the {', '.join(short)} side(s): approaches toward there cannot be fully checked, "
                    "and candidates near that edge may be rated Unassessed."
                )
        target = P_RADIUS if source and "circle" in source else (
            P_DRAWN_AOI if source == "drawn area" else (P_AOI if source else P_DTM)
        )
        parameters[target].setWarningMessage(message)
        AnalyzeHLZ._last_area_km2 = area_km2
        return False

    def execute(self, parameters, messages):
        handler = _ArcpyHandler()
        handler.setLevel(logging.INFO)
        try:
            data_found = None
            if parameters[P_DATA_ROOT].valueAsText:
                data_found = hlz_data.discover_inputs(parameters[P_DATA_ROOT].valueAsText)
                _fill_from_data_root(parameters, data_found)
            if not parameters[P_DTM].valueAsText:
                raise AnalysisError(
                    "No DTM: choose one, or a data folder with exactly one raster in 01_Elevation\\DTM."
                )
            aircraft_path = _aircraft_path(parameters)
            if aircraft_path is None:
                raise ProfileError("No aircraft profile selected.")
            aircraft = load_aircraft_profile(aircraft_path)
            aircraft = _with_size_overrides(parameters, aircraft)
            mission = load_mission_profile(parameters[P_MISSION].valueAsText)
            doctrine = load_doctrine_profile(parameters[P_DOCTRINE].valueAsText)
            check_profile_compatibility(aircraft, mission)

            base = Path(parameters[P_OUT_FOLDER].valueAsText)
            run_name = _safe_name(parameters[P_RUN_NAME].valueAsText)
            run_folder = base / run_name
            resuming = run_folder.exists() and checkpoint_status(run_folder) == "incomplete"
            if not resuming:
                suffix = 2
                while run_folder.exists():
                    run_folder = base / f"{run_name}_{suffix}"
                    suffix += 1
            run_folder.mkdir(parents=True, exist_ok=True)
            configure_logging(run_folder / "hlz_run.log", verbose=False, console=False, append=resuming)
            LOGGER.addHandler(handler)
            LOGGER.info("HLZ Suitability Tool %s | run folder: %s", __version__, run_folder)
            if resuming:
                LOGGER.info("An unfinished run with this name was found: resuming it")
            LOGGER.info("Aircraft: %s | Mission: %s | Doctrine: %s",
                        aircraft.display_name, mission.display_name, doctrine.display_name)
            if data_found is not None:
                LOGGER.info("Data folder: %s", data_found.root)
                LOGGER.info("  DTM: %s", parameters[P_DTM].valueAsText)
                LOGGER.info("  DSM: %s", parameters[P_DSM].valueAsText or "none")
                LOGGER.info("  Exclusion layers: %d", len(_exclusion_layers(parameters[P_EXCLUSION])))
                LOGGER.info("  AOI: %s", parameters[P_AOI].valueAsText or ("drawn on the map" if _has_drawn_aoi(parameters) else "none"))
                LOGGER.info("  Obstacle sources: %d", len(_multi_values(parameters[P_OBST_LAYERS])) + len(_multi_values(parameters[P_OBST_FILES])))

            gdb = str(run_folder / "HLZ_Results.gdb")
            if not arcpy.Exists(gdb):
                arcpy.management.CreateFileGDB(str(run_folder), "HLZ_Results.gdb")
            arcpy.SetProgressor("default", "Preparing inputs...")
            dtm_path = parameters[P_DTM].valueAsText
            dsm_path = parameters[P_DSM].valueAsText
            if raster_info(dtm_path)["sr_type"] == "Geographic":
                if not parameters[P_AUTO_PROJECT].value:
                    raise AnalysisError(
                        "The DTM is in latitude/longitude. Tick 'Project a latitude/longitude DTM or DSM to UTM "
                        "automatically', or project it yourself first."
                    )
                projected_gdb = str(run_folder / "HLZ_Projected.gdb")
                dtm_path = project_geographic_raster(dtm_path, projected_gdb, "DTM", LOGGER.info)
                if dsm_path and raster_info(dsm_path)["sr_type"] == "Geographic":
                    dsm_path = project_geographic_raster(dsm_path, projected_gdb, "DSM", LOGGER.info)
            dtm_sr = raster_info(dtm_path)["sr"]
            centres = _search_centres(parameters, dtm_sr) if _has_centre(parameters) else []
            radius_m = float(parameters[P_RADIUS].value) if parameters[P_RADIUS].value else 0.0
            if centres and radius_m <= 0.0:
                raise AnalysisError("Enter a search radius in metres for the search centre.")
            if centres:
                LOGGER.info("Searching within %.0f m of %d centre point(s)", radius_m, len(centres))
            aoi = parameters[P_AOI].valueAsText
            if aoi and _has_drawn_aoi(parameters):
                raise AnalysisError("Use either the polygon layer or the area drawn on the map, not both.")
            if not aoi:
                aoi = _drawn_aoi_copy(parameters, arcpy.env.scratchGDB)
                if aoi:
                    LOGGER.info("Area of interest: %d polygon(s) drawn on the map", _drawn_feature_count(parameters[P_DRAWN_AOI].value))
            if not aoi and parameters[P_USE_VIEW].value and not centres:
                aoi = view_extent_polygon(dtm_sr)
                if aoi is not None:
                    LOGGER.info("No AOI drawn: using the current map view extent as the AOI")
            reader = ArcpyTileReader(
                dtm_path,
                dsm_path,
                aoi,
                parameters[P_ZUNITS].valueAsText or "Meters",
                arcpy.env.scratchGDB,
                exclusion_layers=_exclusion_layers(parameters[P_EXCLUSION]),
                search_centres=centres or None,
                search_radius_m=radius_m or None,
                log=LOGGER.info,
                line_width_m=float(parameters[P_LINE_WIDTH].value if parameters[P_LINE_WIDTH].value is not None else 10.0),
                point_radius_m=float(parameters[P_POINT_RADIUS].value if parameters[P_POINT_RADIUS].value is not None else 10.0),
                obstacle_layers=_multi_values(parameters[P_OBST_LAYERS]),
                obstacle_files=_multi_values(parameters[P_OBST_FILES]),
                obstacle_height_field=(parameters[P_OBST_FIELD].valueAsText or "").strip() or None,
                obstacle_height_units=parameters[P_OBST_UNITS].valueAsText or "Meters",
                obstacle_default_height_m=float(parameters[P_OBST_DEFAULT].value if parameters[P_OBST_DEFAULT].value is not None else 30.0),
                obstacle_buffer_m=float(parameters[P_OBST_BUFFER].value if parameters[P_OBST_BUFFER].value is not None else 10.0),
            )
            options = RunOptions(
                run_name=run_folder.name,
                oat_c=parameters[P_OAT].value,
                altimeter_inhg=float(parameters[P_ALTIMETER].value or 29.92),
                performance_mode=parameters[P_HOVER].valueAsText or "OGE",
                max_candidates_override=parameters[P_MAX_CANDIDATES].value,
                diagnostic_rasters=parameters[P_DIAGNOSTICS].valueAsText or "AUTO",
                report_basemap=_basemap_choice(parameters[P_BASEMAP]),
                overview_cell_m=(
                    float(parameters[P_OVERVIEW_SIZE].value if parameters[P_OVERVIEW_SIZE].value is not None else 500.0)
                    if parameters[P_OVERVIEW].value else None
                ),
                report_units=_units_choice(parameters[P_REPORT_UNITS]),
                min_spacing_m=float(parameters[P_MIN_SPACING].value or 0.0),
            )
            arcpy.SetProgressor("step", "Analysing tiles...", 0, 100, 1)

            def on_tile(done: int, total: int) -> None:
                try:
                    arcpy.SetProgressorPosition(int(100 * done / max(total, 1)))
                    arcpy.SetProgressorLabel(f"Analysing tile {done} of {total}...")
                except Exception:  # noqa: BLE001
                    pass

            def cancelled() -> bool:
                return bool(getattr(arcpy.env, "isCancelled", False))

            def run(folder: Path):  # type: ignore[no-untyped-def]
                writer = ArcpyWriter(str(folder / "HLZ_Results.gdb"), reader.sr, str(folder / "_tiles" / "rasters"))
                return run_tiled_pipeline(
                    reader,
                    aircraft,
                    mission,
                    doctrine,
                    replace(options, run_name=folder.name),
                    folder,
                    writer,
                    profile_sources={
                        "aircraft": str(aircraft_path),
                        "mission": parameters[P_MISSION].valueAsText,
                        "doctrine": parameters[P_DOCTRINE].valueAsText,
                        **(
                            {"aircraft_size_override": f"landing point {_override_value(parameters[P_TDP_OVERRIDE])} m, ring {_override_value(parameters[P_RING_OVERRIDE])} m (None = from the profile)"}
                            if _override_value(parameters[P_TDP_OVERRIDE]) is not None or _override_value(parameters[P_RING_OVERRIDE]) is not None else {}
                        ),
                    },
                    on_tile=on_tile,
                    cancel_check=cancelled,
                )

            try:
                result = run(run_folder)
            except ResumeMismatch:
                suffix = 2
                fresh = base / f"{run_name}_{suffix}"
                while fresh.exists():
                    suffix += 1
                    fresh = base / f"{run_name}_{suffix}"
                LOGGER.warning(
                    "The unfinished run '%s' used different inputs or settings; starting a new run in '%s'",
                    run_folder.name,
                    fresh.name,
                )
                fresh.mkdir(parents=True)
                arcpy.management.CreateFileGDB(str(fresh), "HLZ_Results.gdb")
                run_folder = fresh
                gdb = str(run_folder / "HLZ_Results.gdb")
                result = run(run_folder)
            arcpy.ResetProgressor()

            arcpy.AddMessage(" ")
            arcpy.AddMessage("=" * 100)
            arcpy.AddMessage(f"RESULTS - {len(result.candidates)} candidates (best first)")
            for line in result.summary_lines:
                arcpy.AddMessage(line)
            arcpy.AddMessage("=" * 100)
            arcpy.AddMessage(f"Geodatabase : {gdb}")
            arcpy.AddMessage(f"HTML report : {result.outputs['report']}")
            arcpy.AddMessage(f"CSV table   : {result.outputs['csv']}")
            if result.outputs.get("rejection_summary"):
                arcpy.AddMessage(f"Rejections  : {result.outputs['rejection_summary']}")
            if result.outputs.get("overview_grid"):
                arcpy.AddMessage(f"Overview grid: {result.outputs['overview_grid']}")
            arcpy.AddMessage(f"Run log     : {run_folder / 'hlz_run.log'}")

            if parameters[P_ADD_MAP].value:
                arcpy.SetProgressorLabel("Adding styled layers to the map...")
                added = add_results_to_map(result.outputs, run_folder.name, arcpy.AddWarning)
                if added:
                    arcpy.AddMessage("Layers added: " + ", ".join(added))
            else:
                arcpy.SetParameterAsText(P_OUT_FC, result.outputs["footprints"])
            arcpy.SetParameterAsText(P_OUT_REPORT, result.outputs["report"])
            arcpy.AddWarning(
                "Planning aid only: confirm every site by ground/aerial reconnaissance and with "
                "the operator's performance data."
            )
            if parameters[P_OPEN_REPORT].value:
                open_file(result.outputs["report"])
        except AnalysisCancelled as error:
            arcpy.AddWarning(str(error))
            raise arcpy.ExecuteError(str(error)) from error
        except (AnalysisError, ProfileError) as error:
            arcpy.AddError(str(error))
            raise arcpy.ExecuteError(str(error)) from error
        finally:
            LOGGER.removeHandler(handler)
            close_logging()
        return

    def postExecute(self, parameters):
        return


class ValidateProfiles:
    """Check edited profile files before using them in an analysis."""

    def __init__(self):
        self.label = "2. Validate Profiles"
        self.description = (
            "Loads aircraft, mission and doctrine profile files with the same strict checks the "
            "analysis uses and lists every value, so edited copies can be verified before use."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        aircraft = arcpy.Parameter(
            displayName="Aircraft profile (.json)",
            name="aircraft_file",
            datatype="DEFile",
            parameterType="Required",
            direction="Input",
        )
        aircraft.filter.list = ["json", "yaml", "yml"]
        aircraft.value = str(profiles_root() / "aircraft" / "generic_medium.json")
        mission = arcpy.Parameter(
            displayName="Mission profile (.json)",
            name="mission_file",
            datatype="DEFile",
            parameterType="Required",
            direction="Input",
        )
        mission.filter.list = ["json", "yaml", "yml"]
        mission.value = str(profiles_root() / "missions" / "screening.json")
        doctrine = arcpy.Parameter(
            displayName="Doctrine profile (.json)",
            name="doctrine_file",
            datatype="DEFile",
            parameterType="Required",
            direction="Input",
        )
        doctrine.filter.list = ["json", "yaml", "yml"]
        doctrine.value = str(profiles_root() / "doctrine" / "generic_planning.json")
        return [aircraft, mission, doctrine]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        loaders = (load_aircraft_profile, load_mission_profile, load_doctrine_profile)
        for parameter, loader in zip(parameters, loaders):
            if parameter.valueAsText:
                try:
                    loader(parameter.valueAsText)
                except ProfileError as error:
                    parameter.setErrorMessage(str(error))
        return

    def execute(self, parameters, messages):
        from dataclasses import asdict

        try:
            aircraft = load_aircraft_profile(parameters[0].valueAsText)
            mission = load_mission_profile(parameters[1].valueAsText)
            doctrine = load_doctrine_profile(parameters[2].valueAsText)
            check_profile_compatibility(aircraft, mission)
        except ProfileError as error:
            arcpy.AddError(str(error))
            raise arcpy.ExecuteError(str(error)) from error
        for title, profile in (("AIRCRAFT", aircraft), ("MISSION", mission), ("DOCTRINE", doctrine)):
            arcpy.AddMessage(f"--- {title} ---")
            for key, value in asdict(profile).items():
                arcpy.AddMessage(f"  {key:<38} {value}")
        if not doctrine.is_approved:
            arcpy.AddWarning("Doctrine profile is not approved: results will be labelled 'Planning only'.")
        arcpy.AddMessage("All profiles are valid.")
        return
