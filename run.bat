@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

:: ============================================================
::  Yolochka Plus - quick launcher
::  Finds Python, force-installs missing libraries, starts app
:: ============================================================

:: 1. Find Python (py launcher first - recommended on Windows)
set "PYTHON_CMD="
for %%C in (py python python3) do (
    if not defined PYTHON_CMD (
        where %%C >nul 2>nul && set "PYTHON_CMD=%%C"
    )
)

if not defined PYTHON_CMD (
    echo [ERROR] Python not found in PATH.
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    echo and CHECK the box "Add Python to PATH" during installation.
    pause
    exit /b 1
)

echo [*] Found Python: %PYTHON_CMD%

:: 2. Check required libraries and force-download whatever is missing
set "MISSING="
for %%M in (PyQt6 deep_translator PIL) do (
    %PYTHON_CMD% -c "import %%M" >nul 2>nul || set "MISSING=!MISSING! %%M"
)

if defined MISSING (
    echo [*] Missing libraries:!MISSING!
    echo [*] Installing them with pip...
    %PYTHON_CMD% -m pip install --upgrade pip
    %PYTHON_CMD% -m pip install PyQt6 deep-translator Pillow
    if errorlevel 1 (
        echo [ERROR] pip failed. Try running this script as Administrator.
        pause
        exit /b 1
    )
    echo [*] Libraries installed successfully.
) else (
    echo [*] All libraries are present.
)

:: 3. Launch the app (pythonw = no console window; fallback to python)
set "RUNNER=%PYTHON_CMD%"
%PYTHON_CMD% -c "import sys; sys.exit(0 if 'pythonw' in sys.executable or __import__('os').path.exists(__import__('sys').executable.replace('python.exe','pythonw.exe')) else 1)" >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%P in ('%PYTHON_CMD% -c "import os,sys;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do (
        if exist "%%P" set "RUNNER=%%P"
    )
)

echo [*] Starting app.py ...
start "" "%RUNNER%" app.py
exit /b 0
