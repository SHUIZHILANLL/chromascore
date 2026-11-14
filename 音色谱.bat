@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem ===========================================================
rem  ChromaScore - local music player with emotion colouring
rem  Drop a music folder (or files) onto this file.
rem  It scans the folder, starts a local server, opens the browser.
rem  Pure ASCII on purpose: a UTF-8 .bat with CJK text gets
rem  mis-parsed by cmd on a GBK console.
rem ===========================================================

if "%~1"=="" (
  echo.
  echo   Drop a MUSIC FOLDER onto this file.
  echo.
  echo   It will scan the folder, analyse every track, and open
  echo   a local player in your browser. The console window must
  echo   stay open while you listen - closing it stops the server.
  echo.
  pause
  exit /b 0
)

set "PY="
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
  "%ProgramFiles%\Python313\python.exe"
  "%ProgramFiles%\Python312\python.exe"
  "%ProgramFiles%\Python311\python.exe"
) do (
  if exist %%P if not defined PY set "PY=%%~P"
)

if not defined PY (
  for /f "delims=" %%I in ('where python 2^>nul ^| findstr /v /i "WindowsApps"') do (
    if not defined PY set "PY=%%I"
  )
)

if not defined PY (
  echo [X] Python not found. Install Python 3.10+ from python.org and retry.
  pause
  exit /b 1
)

echo Python: %PY%
"%PY%" -c "import librosa, numpy, mutagen, PIL" 2>nul
if errorlevel 1 (
  echo Installing dependencies, this runs only once ...
  "%PY%" -m pip install --quiet --upgrade pip
  "%PY%" -m pip install --quiet librosa soundfile numpy mutagen pillow
  if errorlevel 1 (
    echo [X] Dependency install failed. Run manually:
    echo     "%PY%" -m pip install librosa soundfile mutagen pillow
    pause
    exit /b 1
  )
)

set "ARGS="
:collect
if "%~1"=="" goto run
set ARGS=!ARGS! "%~f1"
shift
goto collect

:run
echo.
"%PY%" chromascore.py !ARGS%
if errorlevel 1 (
  echo.
  echo [X] Failed. Read the message above.
)
echo.
pause
