# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Create and verify a deterministic HLZ Suitability Tool v0.10.3 release ZIP."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

VERSION = "v0.10.3"
ROOT_NAME = "HLZ_Suitability_Tool_v0.10.3"
FIXED_TIME = (2026, 10, 8, 0, 0, 0)
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", "logs", ".ruff_cache"}
EXCLUDED_SUFFIXES = {".pyc", ".zip", ".sha256"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def release_files(root: Path) -> list[Path]:
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if EXCLUDED_PARTS.intersection(relative.parts) or path.suffix in EXCLUDED_SUFFIXES:
            continue
        if relative.name == "MANIFEST.sha256":
            continue
        files.append(relative)
    return files


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    output_zip = root.parent / f"{ROOT_NAME}.zip"
    required = {
        "README.md",
        "CHANGELOG.md",
        "LICENSE",
        "VERSION",
        "toolbox/HLZ_Suitability.pyt",
        "hlzcore/pipeline.py",
        "hlzcore/io_arcpy.py",
        "profiles/aircraft/generic_medium.json",
        "tests/run_arcpy_smoke.py",
        "hlzcore/tiling.py",
        "hlzcore/help_text.py",
        "toolbox/HLZ_Suitability.AnalyzeHLZ.pyt.xml",
        "tests/benchmark_large_area.py",
        "tests/check_env.py",
        "tests/run_all_tests.py",
        "RUN_ALL_TESTS.bat",
        "hlzcore/masks.py",
        "hlzcore/obstacles.py",
        "tests/test_obstacles.py",
        "hlzcore/overview.py",
        "tests/test_overview.py",
        "hlzcore/search.py",
        "hlzcore/report_text.py",
        "hlzcore/report_map.py",
        "hlzcore/assets/leaflet/leaflet.js",
        "hlzcore/assets/leaflet/LICENSE-Leaflet-BSD-2-Clause.txt",
        "profiles/aircraft/_catalog/rotorcraft_catalog.csv",
        "profiles/aircraft/canada/ca_ch146.json",
        "scripts/build_aircraft_profiles.py",
        "docs/HLZ_Suitability_User_Manual.docx",
        "docs/HLZ_Suitability_Data_Requirements.docx",
        "docs/HLZ_Suitability_Quick_Start.html",
        "SETUP_HLZ_ENV.bat",
        "hlzcore/data_root.py",
        "tests/test_data_root.py",
        "SETUP_DATA_FOLDER.bat",
        "AUDIT_DATA_FOLDER.bat",
        "toolbox/HLZ_Suitability.PrepareDataFolder.pyt.xml",
    }
    files = release_files(root)
    names = {relative.as_posix() for relative in files}
    missing = sorted(required - names)
    if missing:
        raise FileNotFoundError(f"Release files are missing: {', '.join(missing)}")

    manifest_lines = [
        "# SPDX-License-Identifier: GPL-2.0-or-later",
        "# Copyright (c) 2026 Eui Soo SON",
        f"version: {VERSION}",
        "files:",
    ]
    payload: list[tuple[str, bytes]] = []
    for relative in files:
        data = (root / relative).read_bytes()
        payload.append((relative.as_posix(), data))
        manifest_lines.append(f"{sha256_bytes(data)}  {relative.as_posix()}")
    manifest = ("\n".join(manifest_lines) + "\n").encode("utf-8")
    (root / "MANIFEST.sha256").write_bytes(manifest)
    payload.append(("MANIFEST.sha256", manifest))

    with zipfile.ZipFile(output_zip, "w") as archive:
        for relative, data in sorted(payload):
            info = zipfile.ZipInfo(f"{ROOT_NAME}/{relative}", date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compresslevel=9)

    with zipfile.ZipFile(output_zip) as archive:
        corrupt = archive.testzip()
        if corrupt is not None:
            raise RuntimeError(f"ZIP CRC validation failed for {corrupt}")
        count = len(archive.namelist())
    digest = sha256_bytes(output_zip.read_bytes())
    checksum = output_zip.with_suffix(".zip.sha256")
    checksum.write_text(
        f"{digest}  {output_zip.name}\n"
        "# SPDX-License-Identifier: GPL-2.0-or-later\n"
        "# Copyright (c) 2026 Eui Soo SON\n",
        encoding="utf-8",
    )
    print(f"Created: {output_zip} ({count} entries)")
    print(f"SHA256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
