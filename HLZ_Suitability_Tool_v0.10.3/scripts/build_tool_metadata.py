# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Build the ArcGIS Pro metadata files that provide the (i) tooltips.

ArcGIS Pro shows a parameter's help when the pointer rests on the (i) icon
next to it. For a Python toolbox the text comes from
``<toolbox>.<ToolClass>.pyt.xml`` next to the ``.pyt``. This script writes
those files from ``hlzcore.help_text.FIELD_HELP`` (one source of truth with
the in-dialog guidance).

Run after editing help texts (needs arcpy, or the test fake on PYTHONPATH):
    python scripts\\build_tool_metadata.py
"""

from __future__ import annotations

import html
import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hlzcore import COPYRIGHT, LICENSE_ID, __version__  # noqa: E402
from hlzcore.help_text import FIELD_HELP  # noqa: E402

TOOLBOX = ROOT / "toolbox" / "HLZ_Suitability.pyt"
STAMP = "20261008"

TOOL_SUMMARY = {
    "PrepareDataFolder": (
        "Creates a standard HLZ data folder (HLZ_DATA) with a sub-folder for every input - DTM, DSM, hydrology, "
        "exclusions, AOI, search centres, obstacles, profiles, runs - and audits what is in it: what is missing, what is wrong, "
        "how to fix it, and which files the analysis will use. Nothing is moved or deleted."
    ),
    "AnalyzeHLZ": (
        "Finds and ranks helicopter landing-zone (HLZ) candidates from a bare-earth DTM and an "
        "optional DSM: slope, surface roughness, obstacles in the touchdown point and cleared "
        "ring, approach/departure sectors (including listed towers, masts and wires from FAA DOF, CSV, GeoJSON or GIS layers) and optional density altitude. Water, "
        "wetlands and known hazards (polygons, lines or points) can be excluded, and an optional overview grid shows where the ground passes. Any area size is "
        "processed in tiles; interrupted runs resume. Planning aid only."
    ),
    "ValidateProfiles": (
        "Checks aircraft, mission and doctrine profile files with the same strict rules the "
        "analysis uses and lists every value."
    ),
}
TOOL_USAGE = {
    "PrepareDataFolder": [
        "Run it once to create the folder, copy your data into the sub-folders, then run it again to audit.",
        "The audit report is written to 09_Audit\\HLZ_Data_Audit_latest.html (also .txt and .json).",
        "FAIL items stop the analysis; WARN items lower the quality of the results; INFO items are for your information.",
        "Then choose the data folder in the first field of '1. Analyze HLZ Suitability': empty fields are filled from it.",
    ],
    "AnalyzeHLZ": [
        "Inputs must be in a projected coordinate system with metre horizontal units (e.g. UTM).",
        "Use a DSM whenever possible: without it, trees and buildings are not detected and ratings are capped at Marginal.",
        "No area limit: large areas are split into tiles. Re-run with the same run name to resume an interrupted run.",
        "Results: geodatabase layers (styled in the map), HLZ_Report.html, HLZ_Candidates.csv, run_metadata.json. Tick the overview grid for a coarse HLZ_Overview_Grid layer over a large area.",
        "Planning aid only - confirm every site by reconnaissance and with the operator's performance data.",
    ],
    "ValidateProfiles": ["Copy a shipped profile before editing it; never edit the shipped files in place."],
}
VALIDATE_HELP = {
    "aircraft_file": FIELD_HELP["aircraft_file"],
    "mission_file": FIELD_HELP["mission_file"],
    "doctrine_file": FIELD_HELP["doctrine_file"],
}


def _rich(entry: dict[str, str]) -> str:
    """Escaped HTML block in the format ArcGIS Pro metadata expects."""
    paragraphs = [f"<P><SPAN>{html.escape(entry['what'])}</SPAN></P>"]
    paragraphs.append(f"<P><SPAN><B>Requires:</B> {html.escape(entry['requires'])}</SPAN></P>")
    paragraphs.append(f"<P><SPAN><B>Effect:</B> {html.escape(entry['effect'])}</SPAN></P>")
    if entry.get("example"):
        paragraphs.append(f"<P><SPAN><B>Example:</B> {html.escape(entry['example'])}</SPAN></P>")
    return escape('<DIV STYLE="text-align:Left;">' + "".join(paragraphs) + "</DIV>")


def _plain(text: str) -> str:
    return escape(f'<DIV STYLE="text-align:Left;"><P><SPAN>{html.escape(text)}</SPAN></P></DIV>')


def _load_toolbox():  # type: ignore[no-untyped-def]
    loader = importlib.machinery.SourceFileLoader("hlz_pyt", str(TOOLBOX))
    spec = importlib.util.spec_from_loader("hlz_pyt", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def build_tool(class_name: str, tool, help_map: dict[str, dict[str, str]]) -> str:  # type: ignore[no-untyped-def]
    params = []
    for parameter in tool.getParameterInfo():
        entry = help_map.get(parameter.name) or FIELD_HELP.get(parameter.name)
        if entry is None and parameter.direction == "Output":
            entry = {
                "what": "Derived output set by the tool when it finishes.",
                "requires": "Nothing - filled in automatically.",
                "effect": "Lets ModelBuilder or scripts chain the result.",
            }
        if entry is None:
            continue
        params.append(
            f'<param name="{parameter.name}" displayname="{escape(parameter.displayName)}" '
            f'type="{parameter.parameterType}" direction="{parameter.direction}" '
            f'datatype="{escape(str(parameter.datatype))}" expression="{parameter.name}">'
            f"<dialogReference>{_rich(entry)}</dialogReference>"
            f"<pythonReference>{_rich(entry)}</pythonReference></param>"
        )
    usage = "".join(f"<P><SPAN>{html.escape(item)}</SPAN></P>" for item in TOOL_USAGE[class_name])
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- SPDX-License-Identifier: {LICENSE_ID} -->
<!-- {COPYRIGHT} -->
<metadata xml:lang="en">
<Esri><CreaDate>{STAMP}</CreaDate><CreaTime>00000000</CreaTime><ArcGISFormat>1.0</ArcGISFormat><SyncOnce>TRUE</SyncOnce><ModDate>{STAMP}</ModDate><ModTime>00000000</ModTime></Esri>
<tool name="{class_name}" displayname="{escape(tool.label)}" toolboxalias="hlz" xmlns="">
<arcToolboxHelpPath>c:\\program files\\arcgis\\pro\\Resources\\Help\\gp</arcToolboxHelpPath>
<parameters>{''.join(params)}</parameters>
<summary>{_plain(TOOL_SUMMARY[class_name])}</summary>
<usage>{escape('<DIV STYLE="text-align:Left;">' + usage + '</DIV>')}</usage>
</tool>
<dataIdInfo><idCitation><resTitle>{escape(tool.label)}</resTitle></idCitation>
<idAbs>{_plain(TOOL_SUMMARY[class_name])}</idAbs>
<idCredit>HLZ Suitability Tool {__version__} - {escape(COPYRIGHT)} - {LICENSE_ID}</idCredit>
<searchKeys><keyword>HLZ</keyword><keyword>helicopter landing zone</keyword><keyword>DTM</keyword><keyword>DSM</keyword></searchKeys></dataIdInfo>
<distInfo><distributor><distorFormat><formatName>ArcToolbox Tool</formatName></distorFormat></distributor></distInfo>
<mdHrLv><ScopeCd value="005"/></mdHrLv>
</metadata>
"""


def main() -> int:
    module = _load_toolbox()
    toolbox = module.Toolbox()
    outputs = []
    for tool_class in toolbox.tools:
        tool = tool_class()
        help_map = VALIDATE_HELP if tool_class.__name__ == "ValidateProfiles" else FIELD_HELP
        path = TOOLBOX.with_name(f"HLZ_Suitability.{tool_class.__name__}.pyt.xml")
        path.write_text(build_tool(tool_class.__name__, tool, help_map), encoding="utf-8")
        outputs.append(path)
    box = TOOLBOX.with_name("HLZ_Suitability.pyt.xml")
    box.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- SPDX-License-Identifier: {LICENSE_ID} -->
<!-- {COPYRIGHT} -->
<metadata xml:lang="en">
<Esri><CreaDate>{STAMP}</CreaDate><ArcGISFormat>1.0</ArcGISFormat><SyncOnce>TRUE</SyncOnce></Esri>
<toolbox name="HLZ_Suitability" alias="hlz"><arcToolboxHelpPath>c:\\program files\\arcgis\\pro\\Resources\\Help\\gp</arcToolboxHelpPath><toolsets/></toolbox>
<dataIdInfo><idCitation><resTitle>HLZ Suitability Tool {__version__}</resTitle></idCitation>
<idAbs>{_plain("Helicopter landing-zone screening tools. Start with '0. Set Up and Audit Data Folder', then '1. Analyze HLZ Suitability'.")}</idAbs>
<idCredit>{escape(COPYRIGHT)} - {LICENSE_ID}</idCredit></dataIdInfo>
<distInfo><distributor><distorFormat><formatName>ArcToolbox Toolbox</formatName></distorFormat></distributor></distInfo>
<mdHrLv><ScopeCd value="005"/></mdHrLv>
</metadata>
""",
        encoding="utf-8",
    )
    outputs.append(box)
    for path in outputs:
        print(f"written {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
