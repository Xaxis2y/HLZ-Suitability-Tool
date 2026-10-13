@echo off
REM SPDX-License-Identifier: GPL-2.0-or-later
REM Copyright (c) 2026 Eui Soo SON
REM Audits an HLZ data folder: what is missing, what is wrong, which files the analysis will use.  v0.6.7
REM Usage (ArcGIS Pro "Python Command Prompt" or Anaconda Prompt, after:  activate hlz):
REM     AUDIT_DATA_FOLDER.bat "C:\GIS\HLZ_DATA"  [--aircraft-profile profiles\aircraft\generic_heavy.json]
REM The report is written to <data folder>\09_Audit (HTML, TXT, JSON). Send the TXT or JSON back for review.
setlocal
cd /d "%~dp0"
set "TARGET=%~1"
if "%TARGET%"=="" set /p "TARGET=Data folder to audit [C:\GIS\HLZ_DATA]: "
if "%TARGET%"=="" set "TARGET=C:\GIS\HLZ_DATA"
where python >nul 2>nul
if errorlevel 1 (
  echo ERROR: python was not found. Open the ArcGIS Pro "Python Command Prompt" and run:  activate hlz
  exit /b 1
)
python -m hlzcore.data_root audit "%TARGET%" %2 %3
set "CODE=%ERRORLEVEL%"
echo.
if "%CODE%"=="2" echo NOT READY - fix the FAIL items listed above.
echo Open the report: "%TARGET%\09_Audit\HLZ_Data_Audit_latest.html"
exit /b %CODE%
