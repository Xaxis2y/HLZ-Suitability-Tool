@echo off
REM SPDX-License-Identifier: GPL-2.0-or-later
REM Copyright (c) 2026 Eui Soo SON
REM Core unit tests. Run from the ArcGIS Pro Python Command Prompt after:
REM   activate hlz-arcpy-v04
cd /d "%~dp0"
if not exist logs mkdir logs
python -c "import os,sys; sys.exit(1 if os.path.basename(sys.prefix).lower() in ('base','arcgispro-py3') else 0)"
if errorlevel 1 (
  echo ERROR: do not test in base or arcgispro-py3. Run:  activate hlz-arcpy-v04
  exit /b 1
)
python tests\run_local_tests.py --log logs\hlz_core_test_v0.7.0.log
echo.
echo Return logs\hlz_core_test_v0.7.0.log for review.
