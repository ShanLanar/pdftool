@echo off
title exceltool starten
cd /d "%~dp0"

set "PY=py"
where py >/dev/null 2>/dev/null || set "PY=python"
where %PY% >/dev/null 2>/dev/null
if errorlevel 1 (
    echo FEHLER: Python wurde nicht gefunden.
    echo Bitte Python installieren: https://www.python.org/downloads/
    echo Beim Setup unbedingt "Add Python to PATH" anhaken.
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Erstelle einmalig die virtuelle Umgebung .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo FEHLER: Konnte .venv nicht erstellen.
        echo.
        pause
        exit /b 1
    )
)

set "VPY=.venv\Scripts\python.exe"

"%VPY%" -c "import pandas" >/dev/null 2>/dev/null
if errorlevel 1 (
    echo Installiere benoetigte Pakete in .venv (einmalig, kann etwas dauern) ...
    echo.
    "%VPY%" -m pip install -r requirements.txt
    echo.
)

echo Starte exceltool ...
"%VPY%" exceltool.py
if errorlevel 1 (
    echo.
    echo Das Programm wurde mit einem Fehler beendet (siehe oben).
    pause
)
exit /b 0
