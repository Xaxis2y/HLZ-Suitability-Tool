# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Run the core unit tests and write a step-by-step log for review.

Usage (activated dedicated environment, e.g. the hlz-arcpy-v04 clone):
    python tests\\run_local_tests.py --log logs\\hlz_core_test_v0.10.4.log
"""

from __future__ import annotations

import argparse
import importlib
import platform
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default=str(ROOT / "logs" / "hlz_core_test_v0.10.4.log"))
    args = parser.parse_args()
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# SPDX-License-Identifier: GPL-2.0-or-later",
        "# Copyright (c) 2026 Eui Soo SON",
        "HLZ Suitability Tool v0.10.4 - core test log",
        f"started: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"python: {sys.version}",
        f"executable: {sys.executable}",
        f"platform: {platform.platform()}",
        f"conda env: {Path(sys.prefix).name}",
    ]
    for module in ("numpy", "scipy", "matplotlib", "arcpy", "rasterio"):
        try:
            imported = importlib.import_module(module)
            version = getattr(imported, "__version__", None)
            if module == "arcpy":
                version = imported.GetInstallInfo().get("Version", "?")
            lines.append(f"module {module}: {version}")
        except Exception as error:  # noqa: BLE001
            lines.append(f"module {module}: NOT AVAILABLE ({type(error).__name__})")
    if Path(sys.prefix).name.lower() in {"base", "arcgispro-py3"}:
        lines.append("WARNING: running in a shared base environment; use a dedicated clone")

    import test_core
    import test_data_root
    import test_obstacles
    import test_overview
    import test_v071
    import test_v080
    import test_v090
    import test_v0100

    passed = failed = 0
    tests = [(module, name) for module in (test_core, test_data_root, test_obstacles, test_overview, test_v071, test_v080, test_v090, test_v0100) for name in sorted(dir(module))]
    for module, name in tests:
        function = getattr(module, name)
        if not (name.startswith("test_") and callable(function)):
            continue
        started = time.perf_counter()
        try:
            function()
            passed += 1
            lines.append(f"PASS {name} ({time.perf_counter() - started:.2f} s)")
        except Exception:  # noqa: BLE001
            failed += 1
            lines.append(f"FAIL {name} ({time.perf_counter() - started:.2f} s)")
            lines.extend("    " + row for row in traceback.format_exc().splitlines())
    lines.append(f"SUMMARY passed={passed} failed={failed}")
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nLog written to {log_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
