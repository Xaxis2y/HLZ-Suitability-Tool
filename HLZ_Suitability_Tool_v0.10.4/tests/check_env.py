# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Check that the active Python environment is fit for HLZ testing (v0.10.4).

Run inside the dedicated environment (never ``base`` or ``arcgispro-py3``):

    python tests\\check_env.py

It reports the interpreter, the packages, the PC (CPU, RAM, free disk) and
lists errors (the tests cannot run) and warnings (they can, but results or
speed may suffer). ``RUN_ALL_TESTS.bat`` runs the same check first.
"""

from __future__ import annotations

import argparse
import importlib
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hlzcore import __version__  # noqa: E402
from hlzcore.help_text import path_warning  # noqa: E402

SHARED_ENVS = {"base", "arcgispro-py3"}
ALL_STEPS = ("core", "smoke", "benchmark", "cli")
MINIMUM_RAM_GB = 8.0
MINIMUM_FREE_DISK_GB = 10.0


def total_ram_gb() -> float | None:
    """Physical memory in GB (Windows via ctypes, Linux/macOS via sysconf)."""
    try:
        if os.name == "nt":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [  # noqa: RUF012
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
            return status.ullTotalPhys / 1024**3
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
    except Exception:  # noqa: BLE001
        return None


def module_version(name: str) -> str | None:
    """Version string of an importable module, or None when it is not installed."""
    try:
        module = importlib.import_module(name)
    except Exception:  # noqa: BLE001
        return None
    if name == "arcpy":
        try:
            return str(module.GetInstallInfo().get("Version", "?"))
        except Exception:  # noqa: BLE001
            return "?"
    return str(getattr(module, "__version__", "?"))


def collect(root: Path = ROOT) -> dict[str, Any]:
    return {
        "tool_version": __version__,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "env_name": Path(sys.prefix).name,
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "ram_gb": total_ram_gb(),
        "disk_free_gb": shutil.disk_usage(root).free / 1024**3,
        "root": str(root),
        "modules": {name: module_version(name) for name in ("numpy", "scipy", "matplotlib", "arcpy", "rasterio")},
    }


def problems(info: dict[str, Any], steps: tuple[str, ...] = ALL_STEPS) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for the planned test steps."""
    errors: list[str] = []
    warnings: list[str] = []
    modules = info["modules"]
    if str(info["env_name"]).lower() in SHARED_ENVS:
        errors.append(
            f"This is the shared environment '{info['env_name']}'. Never test in it. Activate a "
            "dedicated clone: activate hlz   (create it once with SETUP_HLZ_ENV.bat)."
        )
    if tuple(int(part) for part in str(info["python"]).split(".")[:2]) < (3, 9):
        errors.append(f"Python {info['python']} is too old; 3.9 or newer is required.")
    for name in ("numpy", "scipy"):
        if modules.get(name) is None:
            errors.append(f"{name} is not installed in this environment.")
    if "smoke" in steps and modules.get("arcpy") is None:
        errors.append(
            "arcpy is not available, so the ArcGIS Pro smoke test cannot run. Open the ArcGIS Pro "
            "'Python Command Prompt' (not a regular Anaconda Prompt) and activate the clone there."
        )
    if modules.get("arcpy") is not None and modules.get("matplotlib") is None:
        warnings.append("matplotlib is missing: polygon AOIs cannot be rasterised for tile edges.")
    if "cli" in steps and modules.get("rasterio") is None:
        warnings.append(
            "rasterio is not installed, so the optional command-line test will be skipped (the ArcGIS "
            "Pro tests do not need it)."
        )
    message = path_warning(str(info["root"]))
    if message:
        warnings.append(message)
    ram = info.get("ram_gb")
    if ram is not None and ram < MINIMUM_RAM_GB:
        warnings.append(
            f"Only {ram:.1f} GB of RAM: the default 16 M-cell tiles peak at about 2.4 GB, so set "
            "max_input_cells to 4,000,000 in a copied mission profile."
        )
    if info["disk_free_gb"] < MINIMUM_FREE_DISK_GB:
        warnings.append(f"Only {info['disk_free_gb']:.1f} GB free on this drive; city-scale runs need tens of GB.")
    return errors, warnings


def report_lines(info: dict[str, Any]) -> list[str]:
    ram = info["ram_gb"]
    lines = [
        f"HLZ Suitability Tool {info['tool_version']} - environment report",
        f"python      : {info['python']}  ({info['executable']})",
        f"environment : {info['env_name']}",
        f"platform    : {info['platform']}",
        f"cpu / ram   : {info['cpu_count']} logical CPUs / {'unknown' if ram is None else f'{ram:.1f} GB'}",
        f"free disk   : {info['disk_free_gb']:.1f} GB  (tool folder: {info['root']})",
    ]
    for name, version in info["modules"].items():
        lines.append(f"module {name:<10}: {version if version is not None else 'NOT AVAILABLE'}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--steps", default=",".join(ALL_STEPS), help="comma-separated: core,smoke,benchmark,cli")
    parser.add_argument("--allow-no-arcpy", action="store_true", help="do not treat a missing arcpy as an error")
    args = parser.parse_args(argv)
    steps = tuple(item.strip() for item in args.steps.split(",") if item.strip())
    info = collect()
    errors, warnings = problems(info, steps)
    if args.allow_no_arcpy:
        errors = [item for item in errors if not item.startswith("arcpy is not available")]
    print("\n".join(report_lines(info)))
    for item in warnings:
        print(f"WARNING: {item}")
    for item in errors:
        print(f"ERROR  : {item}")
    print("RESULT : " + ("NOT READY" if errors else "READY") + f" for steps: {', '.join(steps)}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
