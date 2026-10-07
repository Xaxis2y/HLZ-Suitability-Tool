@echo off
REM SPDX-License-Identifier: GPL-2.0-or-later
REM Copyright (c) 2026 Eui Soo SON
REM Runs EVERY test in the active environment and bundles the logs into one zip.
REM Run from the ArcGIS Pro "Python Command Prompt" after:   activate hlz
REM Options are passed through, e.g.  RUN_ALL_TESTS.bat --quick
cd /d "%~dp0"
if not exist logs mkdir logs
python -c "import os,sys; sys.exit(1 if os.path.basename(sys.prefix).lower() in ('base','arcgispro-py3') else 0)"
if errorlevel 1 (
  echo ERROR: do not test in base or arcgispro-py3. Run:  activate hlz
  echo        ^(create it once with SETUP_HLZ_ENV.bat^)
  exit /b 1
)
python tests\run_all_tests.py %*
echo.
echo Send back the ONE file shown above:  logs\hlz_test_logs_v0.7.0.zip
