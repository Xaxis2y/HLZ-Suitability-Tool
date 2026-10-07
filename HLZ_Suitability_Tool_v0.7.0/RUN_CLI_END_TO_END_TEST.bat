@echo off
REM SPDX-License-Identifier: GPL-2.0-or-later
REM Copyright (c) 2026 Eui Soo SON
REM OPTIONAL command-line end-to-end test. Run from Anaconda Prompt after:
REM   conda env create -f environment.yml
REM   conda activate hlz-cli-v04
cd /d "%~dp0"
if not exist logs mkdir logs
python -c "import os,sys; sys.exit(1 if os.path.basename(sys.prefix).lower() in ('base','arcgispro-py3') else 0)"
if errorlevel 1 (
  echo ERROR: do not test in base. Run:  conda activate hlz-cli-v04
  exit /b 1
)
python tests\run_end_to_end.py --log logs\hlz_end_to_end_v0.7.0.log
echo.
echo Return logs\hlz_end_to_end_v0.7.0.log for review.
