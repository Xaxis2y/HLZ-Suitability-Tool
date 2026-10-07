@echo off
REM SPDX-License-Identifier: GPL-2.0-or-later
REM Copyright (c) 2026 Eui Soo SON
REM Creates (once) the dedicated environment "hlz" for ALL HLZ testing and checks it.
REM Run from the ArcGIS Pro "Python Command Prompt".  Usage:  SETUP_HLZ_ENV.bat [name]
REM Nothing is installed: the new environment is a clone, so arcgispro-py3 stays untouched.
setlocal
cd /d "%~dp0"
set "ENVNAME=%~1"
if "%ENVNAME%"=="" set "ENVNAME=hlz"

where conda >nul 2>nul
if errorlevel 1 (
  echo ERROR: conda was not found. Open the ArcGIS Pro "Python Command Prompt" ^(Start menu, ArcGIS folder^).
  exit /b 1
)

call conda env list | findstr /b /c:"%ENVNAME% " >nul
if not errorlevel 1 (
  echo Environment "%ENVNAME%" already exists - skipping creation.
  goto check
)

call conda env list | findstr /b /c:"hlz-arcpy-v04 " >nul
if not errorlevel 1 (
  echo Cloning your tested environment "hlz-arcpy-v04" to "%ENVNAME%" ...
  call conda create --clone hlz-arcpy-v04 --name %ENVNAME% --yes
) else (
  echo Cloning "arcgispro-py3" to "%ENVNAME%" ^(several GB, a few minutes^) ...
  call conda create --clone arcgispro-py3 --name %ENVNAME% --pinned --yes
)
if errorlevel 1 (
  echo ERROR: the clone failed. Copy the messages above and send them to me.
  exit /b 1
)

:check
echo.
echo Checking "%ENVNAME%" ...
call activate %ENVNAME%
python tests\check_env.py --steps core,smoke,benchmark
if errorlevel 1 (
  echo.
  echo The environment is NOT ready - see the ERROR lines above.
  exit /b 1
)
echo.
echo Done. From now on, for every test:
echo     activate %ENVNAME%
echo     cd /d "%~dp0"
echo     RUN_ALL_TESTS.bat
echo You may delete the old environment later with:  conda remove --name hlz-arcpy-v04 --all
