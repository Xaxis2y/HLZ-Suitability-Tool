# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Generate the named aircraft profiles from profiles/aircraft/_catalog/rotorcraft_catalog.csv.

    python scripts\\build_aircraft_profiles.py

Each catalog row becomes <group>/<key>.json. Class limits (slope, roughness, obstacle heights,
approach range, approach angles, clearance height) come from the generic light / medium / heavy
profiles, so editing a generic profile and re-running updates every named aircraft of that class.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hlzcore import COPYRIGHT, LICENSE_ID, __version__  # noqa: E402
from hlzcore.config import ProfileError, load_aircraft_profile  # noqa: E402

AIRCRAFT = ROOT / "profiles" / "aircraft"
CATALOG = AIRCRAFT / "_catalog" / "rotorcraft_catalog.csv"
LIGHT_MAX_KG = 4500.0
MEDIUM_MAX_KG = 14000.0
CLASS_FIELDS = (
    "tdp_diameter_m", "cleared_ring_m", "max_landing_slope_deg", "max_upslope_landing_deg", "max_roughness_m",
    "tdp_obstacle_height_m", "ring_obstacle_height_m", "approach_range_m", "corridor_half_width_deg",
    "suitable_angle_deg", "unsuitable_angle_deg", "clearance_height_m",
)
NOTE = (
    "PLANNING DEFAULTS ONLY - limits come from the generic weight class, with the touchdown-point size "
    "taken from the catalog; replace every value with operator- or authority-approved data before operational use."
)


def weight_class(mtow_kg: float) -> str:
    return "light" if mtow_kg <= LIGHT_MAX_KG else "medium" if mtow_kg <= MEDIUM_MAX_KG else "heavy"


def number(text: str) -> float | None:
    text = (text or "").strip()
    return float(text) if text else None


def main() -> int:
    classes = {name: json.loads((AIRCRAFT / f"generic_{name}.json").read_text(encoding="utf-8")) for name in ("light", "medium", "heavy")}
    rows = list(csv.DictReader(CATALOG.open(encoding="utf-8-sig", newline="")))
    summary = []
    problems = 0
    for row in rows:
        mtow = float(row["mtow_kg"])
        rotor = float(row["main_rotor_diameter_m"])
        length = float(row["overall_length_m"])
        cls = weight_class(mtow)
        base = classes[cls]
        profile = {key: base[key] for key in CLASS_FIELDS}
        tdp_override = number(row["tdp_diameter_m_override"])
        ring_override = number(row["cleared_ring_m_override"])
        profile["tdp_diameter_m"] = tdp_override or base["tdp_diameter_m"]
        profile["cleared_ring_m"] = ring_override or base["cleared_ring_m"]
        footprint = max(rotor, length)
        if profile["tdp_diameter_m"] < 1.5 * footprint:
            print(f"WARNING {row['key']}: TDP {profile['tdp_diameter_m']:.0f} m is under 1.5 x the aircraft footprint ({footprint:.1f} m)")
            problems += 1
        tdp_basis = row["tdp_basis"] if tdp_override else f"weight class {cls} (MTOW {mtow:,.0f} kg)"
        designation, common = row["designation"], row["name"]
        if designation.lower() in common.lower():
            name = common
        elif common.lower() in designation.lower():
            name = designation
        else:
            name = f"{designation} {common}"
        document = {
            "_license": f"SPDX-License-Identifier: {LICENSE_ID}",
            "_copyright": COPYRIGHT,
            "_notes": NOTE,
            "_generated_by": f"scripts/build_aircraft_profiles.py ({__version__}) from rotorcraft_catalog.csv",
            "_catalog_key": row["key"],
            "_operators": row["operators"],
            "_data_status": row["data_status"],
            "_source": row["source"],
            "_tdp_basis": tdp_basis,
            "_weight_class": cls,
            "_rotor_diameter_m": rotor,
            "_overall_length_m": length,
            "_length_basis": row["length_basis"],
            "_mtow_kg": mtow,
            "profile_id": f"{row['key']}-{__version__}",
            "display_name": name,
            "description": (
                f"{name} ({row['manufacturer']}, {row['role']}). Rotor {rotor:g} m, length {length:g} m ({row['length_basis']}), "
                f"MTOW {mtow:,.0f} kg. Touchdown point {profile['tdp_diameter_m']:g} m: {tdp_basis}. {row['notes']} "
                f"Data status: {row['data_status']}. {NOTE}"
            ).strip(),
            **profile,
            "hover_ceiling_ige_ft": number(row["hover_ceiling_ige_ft"]),
            "hover_ceiling_oge_ft": number(row["hover_ceiling_oge_ft"]),
        }
        folder = AIRCRAFT / row["group"]
        folder.mkdir(exist_ok=True)
        path = folder / f"{row['key']}.json"
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        try:
            loaded = load_aircraft_profile(path)
        except ProfileError as error:
            print(f"ERROR {row['key']}: {error}")
            problems += 1
            continue
        summary.append((row["group"], name, cls, loaded.tdp_diameter_m, loaded.cleared_ring_m, loaded.approach_range_m, row["data_status"].split(" ")[0]))
    print(f"{len(summary)} of {len(rows)} profiles written and validated")
    print(f"{'group':<14}{'aircraft':<30}{'class':<8}{'TDP m':>6}{'ring m':>7}{'range m':>8}  status")
    for group, name, cls, tdp, ring, rng, status in summary:
        print(f"{group:<14}{name:<30}{cls:<8}{tdp:>6.0f}{ring:>7.0f}{rng:>8.0f}  {status}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
