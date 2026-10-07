@echo off
REM SPDX-License-Identifier: GPL-2.0-or-later
REM Copyright (c) 2026 Eui Soo SON
REM Creates the HLZ data folder (missing parts only) and audits it.  v0.7.0
REM Usage (ArcGIS Pro "Python Command Prompt" or Anaconda Prompt, after:  activate hlz):
REM     SETUP_DATA_FOLDER.bat                      asks for the folder (default C:\GIS\HLZ_DATA)
REM     SETUP_DATA_FOLDER.bat "D:\GIS\HLZ_DATA"
REM Nothing is installed, moved or deleted. The audit uses only the Python standard library.
setlocal
cd /d "%~dp0"
set "TARGET=%~1"
if "%TARGET%"=="" set /p "TARGET=Data folder to create [C:\GIS\HLZ_DATA]: "
if "%TARGET%"=="" set "TARGET=C:\GIS\HLZ_DATA"
where python >nul 2>nul
if errorlevel 1 (
  echo ERROR: python was not found. Open the ArcGIS Pro "Python Command Prompt" and run:  activate hlz
  exit /b 1
)
python -c "import os,sys; sys.exit(1 if os.path.basename(sys.prefix).lower() in ('base','arcgispro-py3') else 0)"
if errorlevel 1 echo NOTE: you are in a shared environment. This step installs nothing, but use "activate hlz" for testing.
python -m hlzcore.data_root init "%TARGET%"
set "CODE=%ERRORLEVEL%"
echo.
if "%CODE%"=="2" echo The folder is ready to fill. Copy your DTM, DSM and water polygons into it, then run AUDIT_DATA_FOLDER.bat "%TARGET%"
if "%CODE%"=="0" echo The data folder is READY. In ArcGIS Pro choose it in the first field of "1. Analyze HLZ Suitability".
echo Report: "%TARGET%\09_Audit\HLZ_Data_Audit_latest.html"
exit /b %CODE%
