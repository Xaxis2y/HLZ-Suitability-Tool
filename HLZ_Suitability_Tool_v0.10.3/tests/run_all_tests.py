# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 Eui Soo SON
"""Run every HLZ test in the ACTIVE environment and bundle the logs (v0.10.3).

    python tests\\run_all_tests.py                 # everything (10 km benchmark)
    python tests\\run_all_tests.py --quick         # 3 km benchmark, faster
    python tests\\run_all_tests.py --steps core,smoke

Steps (each runs in its own process, so one crash never hides the others):

    core       unit tests (NumPy/SciPy only)
    smoke      real ArcPy test of the toolbox, 6 scenarios (needs arcpy)
    benchmark  large-area speed/memory test with a procedural DTM
    cli        optional command-line end-to-end test (needs rasterio; skipped if absent)

Output: logs\\hlz_test_logs_<version>.zip - send that single file back.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import check_env  # noqa: E402
from hlzcore import COPYRIGHT, LICENSE_ID, __version__  # noqa: E402

STEP_TIMEOUT_S = {"core": 1800, "smoke": 5400, "benchmark": 10800, "cli": 3600}
KEY_LINE = re.compile(r"SUMMARY|done in|peak traced|estimate at this speed|tiles \d+ \| candidates|FAIL ")


@dataclass
class StepResult:
    name: str
    status: str  # PASS | FAIL | TIMEOUT | SKIPPED | ERROR
    seconds: float = 0.0
    detail: str = ""
    log_file: Path | None = None
    console_file: Path | None = None
    key_lines: list[str] = field(default_factory=list)


def step_command(name: str, log_dir: Path, km: float) -> tuple[list[str], Path]:
    version = __version__
    py = sys.executable
    tests = ROOT / "tests"
    if name == "core":
        log = log_dir / f"hlz_core_test_{version}.log"
        return [py, str(tests / "run_local_tests.py"), "--log", str(log)], log
    if name == "smoke":
        log = log_dir / f"hlz_arcpy_smoke_{version}.log"
        return [py, str(tests / "run_arcpy_smoke.py"), "--log", str(log)], log
    if name == "benchmark":
        log = log_dir / f"hlz_benchmark_{version}.log"
        return [py, str(tests / "benchmark_large_area.py"), "--km", f"{km:g}", "--log", str(log)], log
    if name == "cli":
        log = log_dir / f"hlz_end_to_end_{version}.log"
        return [py, str(tests / "run_end_to_end.py"), "--log", str(log)], log
    raise ValueError(f"unknown step {name!r}")


def run_logged(command: list[str], console_file: Path, timeout_s: int) -> tuple[int | None, float]:
    """Run a command, echo its output live and save it. Returns (exit code or None on timeout, seconds)."""
    environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    started = time.perf_counter()
    with console_file.open("w", encoding="utf-8", errors="replace") as sink:
        process = subprocess.Popen(  # noqa: S603 - fixed command list built above
            command,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )

        def pump() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                sink.write(line)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            code: int | None = process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            code = None
        reader.join(timeout=10)
    return code, time.perf_counter() - started


def extract_key_lines(*texts: str, limit: int = 8) -> list[str]:
    """Pick the lines worth showing in the summary (totals, timings, failures)."""
    found: list[str] = []
    for text in texts:
        for line in text.splitlines():
            if KEY_LINE.search(line) and line.strip() not in found:
                found.append(line.strip())
    return found[:limit]


def read_text(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--steps", default="core,smoke,benchmark,cli")
    parser.add_argument("--km", type=float, default=10.0, help="benchmark area edge in km (default 10 = 100 M cells)")
    parser.add_argument("--quick", action="store_true", help="3 km benchmark instead of 10 km")
    parser.add_argument("--force", action="store_true", help="run even if the environment check reports errors")
    parser.add_argument("--log-dir", default=str(ROOT / "logs"))
    args = parser.parse_args(argv)
    km = 3.0 if args.quick else args.km
    steps = tuple(item.strip() for item in args.steps.split(",") if item.strip())
    unknown = [item for item in steps if item not in check_env.ALL_STEPS]
    if unknown:
        print(f"Unknown step(s): {', '.join(unknown)}. Valid: {', '.join(check_env.ALL_STEPS)}")
        return 2
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    # 1. Environment ------------------------------------------------------------
    info = check_env.collect()
    errors, warnings = check_env.problems(info, steps)
    env_lines = check_env.report_lines(info)
    env_lines += [f"WARNING: {item}" for item in warnings] + [f"ERROR  : {item}" for item in errors]
    env_file = log_dir / f"hlz_env_report_{__version__}.log"
    env_file.write_text(
        f"# SPDX-License-Identifier: {LICENSE_ID}\n# {COPYRIGHT}\n" + "\n".join(env_lines) + "\n", encoding="utf-8"
    )
    print("\n".join(env_lines))
    if errors and not args.force:
        print("\nThe environment is not ready (see ERROR above). Nothing was run. Fix it, or use --force.")
        return 2

    # 2. Steps ----------------------------------------------------------------
    results: list[StepResult] = []
    for name in steps:
        print(f"\n{'=' * 78}\nSTEP {name.upper()}\n{'=' * 78}", flush=True)
        if name == "cli" and info["modules"].get("rasterio") is None:
            results.append(StepResult(name, "SKIPPED", detail="rasterio not installed (optional command-line test)"))
            print("SKIPPED: rasterio is not installed (this step is optional).")
            continue
        command, log_file = step_command(name, log_dir, km)
        console_file = log_dir / f"hlz_console_{name}_{__version__}.txt"
        code, seconds = run_logged(command, console_file, STEP_TIMEOUT_S[name])
        if code is None:
            status, detail = "TIMEOUT", f"stopped after {STEP_TIMEOUT_S[name] // 60} min"
        elif code == 0:
            status, detail = "PASS", ""
        else:
            status, detail = "FAIL", f"exit code {code}"
        result = StepResult(name, status, seconds, detail, log_file, console_file)
        result.key_lines = extract_key_lines(read_text(log_file), read_text(console_file))
        results.append(result)

    # 3. Summary --------------------------------------------------------------
    lines = [
        f"# SPDX-License-Identifier: {LICENSE_ID}",
        f"# {COPYRIGHT}",
        f"HLZ Suitability Tool {__version__} - test summary ({time.strftime('%Y-%m-%d %H:%M:%S')})",
        f"environment: {info['env_name']} | python {info['python']} | arcpy {info['modules'].get('arcpy')}",
        f"pc: {info['cpu_count']} CPUs, {info['ram_gb'] and round(info['ram_gb'], 1)} GB RAM, "
        f"{info['disk_free_gb']:.0f} GB free",
        "",
    ]
    for result in results:
        minutes = result.seconds / 60
        timing = f"{minutes:5.1f} min" if result.seconds else "      - "
        lines.append(f"{result.name:<10} {result.status:<8} {timing}  {result.detail}")
        lines += [f"    {item}" for item in result.key_lines]
    ran = [item for item in results if item.status != "SKIPPED"]
    all_ok = bool(ran) and all(item.status == "PASS" for item in ran)
    lines += ["", "OVERALL: " + ("ALL PASSED" if all_ok else "SOMETHING FAILED - see the logs")]
    summary_file = log_dir / f"hlz_test_summary_{__version__}.txt"
    summary_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    bundle = log_dir / f"hlz_test_logs_{__version__}.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(summary_file, summary_file.name)
        archive.write(env_file, env_file.name)
        for result in results:
            for path in (result.log_file, result.console_file):
                if path is not None and path.is_file():
                    archive.write(path, path.name)
    print("\n" + "\n".join(lines[3:]))
    print(f"\nSend this ONE file back: {bundle}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
