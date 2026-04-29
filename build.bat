@echo off
title PIA Scrap - Build EXE
cd /d "%~dp0"

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [error] Python is not installed or not in PATH.
    pause
    exit /b 1
)

echo [info] Installing dependencies...
pip install -r requirements.txt
pip install pyinstaller

echo.
echo [info] Building PIA-Scrap.exe...
pyinstaller pia-scrap.spec --noconfirm

if %errorlevel% equ 0 (
    echo.
    echo [success] Build complete: dist\PIA-Scrap.exe
) else (
    echo.
    echo [error] Build failed.
)
pause
