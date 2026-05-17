@echo off
title Minecraft Autoclicker Builder

echo ============================================================
echo     Minecraft Autoclicker by Rane - Executable Builder
echo ============================================================
echo.

:: Check if required files exist
echo [1/5] Checking required files...

set MISSING=0
if not exist "Minecraft_Autoclicker.py" (
    echo   ERROR: Missing Minecraft_Autoclicker.py
    set MISSING=1
)
if not exist "click.ico" (
    echo   ERROR: Missing click.ico
    set MISSING=1
)
if not exist "start.wav" (
    echo   ERROR: Missing start.wav
    set MISSING=1
)
if not exist "stop.wav" (
    echo   ERROR: Missing stop.wav
    set MISSING=1
)

if %MISSING% EQU 1 (
    echo.
    echo Build aborted. Missing required files.
    pause
    exit /b 1
)

echo   All required files found.
echo.

:: Check Python
echo [2/5] Checking Python installation...

python --version >nul 2>&1
if errorlevel 1 (
    echo   ERROR: Python not found or not in PATH
    echo   Please install Python first.
    pause
    exit /b 1
)
echo   Python found.
echo.

:: Install PyInstaller if needed
echo [3/5] Checking PyInstaller...

pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo   Installing PyInstaller...
    pip install pyinstaller
    if errorlevel 1 (
        echo   ERROR: Failed to install PyInstaller
        pause
        exit /b 1
    )
) else (
    echo   PyInstaller is installed.
)
echo.

:: Install dependencies
echo [4/5] Checking runtime dependencies...

pip show pynput >nul 2>&1
if errorlevel 1 (
    echo   Installing pynput...
    pip install pynput
)

pip show PyQt6 >nul 2>&1
if errorlevel 1 (
    echo   Installing PyQt6...
    pip install PyQt6
)
echo   Dependencies ready.
echo.

:: Build the executable
echo [5/5] Building executable...
echo   This may take 1-2 minutes...

pyinstaller --onefile --noconsole --name "Minecraft_Autoclicker" --icon "click.ico" --add-data "start.wav;." --add-data "stop.wav;." --add-data "click.ico;." --hidden-import PyQt6 --hidden-import PyQt6.QtCore --hidden-import PyQt6.QtGui --hidden-import PyQt6.QtWidgets --hidden-import pynput --hidden-import pynput.keyboard --collect-all PyQt6 "Minecraft_Autoclicker.py"

if errorlevel 1 (
    echo.
    echo ============================================================
    echo     BUILD FAILED!
    echo ============================================================
    echo.
    pause
    exit /b 1
)

:: Success message
echo.
echo ============================================================
echo     BUILD SUCCESSFUL!
echo ============================================================
echo.
echo Executable created at: dist\Minecraft_Autoclicker.exe
echo.
echo The .exe is standalone - you can move it anywhere.
echo All assets (icon, sounds) are embedded inside.
echo.
echo To run: double-click dist\Minecraft_Autoclicker.exe
echo.
pause