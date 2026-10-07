SPDX-License-Identifier: GPL-2.0-or-later
Copyright (c) 2026 Eui Soo SON

ROTORCRAFT CATALOG
==================
rotorcraft_catalog.csv is the single, editable source for the named aircraft profiles
in the folders canada\, united_states\ and nato_europe\ (one JSON profile per row).

To change or add an aircraft
  1. Open rotorcraft_catalog.csv in Excel (it is UTF-8). Edit a row or add one.
  2. Run, from the release folder:   python scripts\build_aircraft_profiles.py
  3. The profiles are regenerated; each is validated with the same strict checks as the tool.

Which columns drive the analysis
  tdp_diameter_m_override  The touchdown-point diameter. Blank = the weight-class value
                           (light 25 m, medium 50 m, heavy 80 m).
  cleared_ring_m_override  Blank = the weight-class value (10 / 20 / 30 m).
  hover_ceiling_ige_ft, hover_ceiling_oge_ft
                           From the aircraft performance charts. Blank = not used. They are the
                           only columns that make density altitude reject a site.
  mtow_kg                  Sets the weight class: light up to 4,500 kg, medium up to 14,000 kg,
                           heavy above that.
Every other numeric limit (slope, roughness, obstacle heights, approach range, approach angles)
comes from the matching GENERIC class profile (generic_light / generic_medium / generic_heavy).

Reference columns (not used in the analysis): rotor diameter, overall length, length basis,
operators, source, data_status, notes.

IMPORTANT
  * These are planning defaults. The release contains NO classified or restricted doctrine.
    Replace the values with those approved by the aircraft operator or your authority.
  * data_status says whether a record was checked against an official page when this
    catalog was built ("verified") or is reference data that still needs checking.
  * A TDP override from FM 3-21.38 was read from a training summary of that manual, not
    from the manual itself.
