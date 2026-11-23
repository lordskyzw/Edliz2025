@echo off
REM Double-click this file to set up and start EDLIZ Assistant.
REM It hands over to scripts\setup-windows.ps1 with the execution policy
REM bypassed, because Windows will not run a .ps1 by double-clicking.

setlocal
cd /d "%~dp0"

if not exist "scripts\setup-windows.ps1" (
    echo.
    echo   Could not find scripts\setup-windows.ps1
    echo   Make sure you unzipped the whole folder and kept its structure.
    echo.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\setup-windows.ps1"

if errorlevel 1 (
    echo.
    echo   Setup did not finish. The messages above say why.
    pause
)
