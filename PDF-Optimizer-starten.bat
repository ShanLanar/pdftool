@echo off
title PDF-Optimizer starten
cd /d "%~dp0"

REM Python finden: bevorzugt den Windows "py"-Launcher, sonst "python"
set "PY=py"
where py >nul 2>nul || set "PY=python"
where %PY% >nul 2>nul
if errorlevel 1 (
    echo FEHLER: Python wurde nicht gefunden.
    echo Bitte Python installieren: https://www.python.org/downloads/
    echo Beim Setup unbedingt "Add Python to PATH" anhaken.
    echo.
    pause
    exit /b 1
)

REM Eigene virtuelle Umgebung (.venv) anlegen, falls noch nicht vorhanden.
REM So landen die Pakete NICHT im globalen Python und stoeren keine anderen Tools.
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

REM Pakete installieren/aktualisieren, wenn pypdf fehlt ODER sich
REM requirements.txt seit der letzten Installation geaendert hat.
set "NEED_INSTALL="
"%VPY%" -c "import pypdf" >nul 2>nul || set "NEED_INSTALL=1"
fc /b requirements.txt ".venv\requirements.stamp" >nul 2>nul || set "NEED_INSTALL=1"
if defined NEED_INSTALL (
    echo Installiere/aktualisiere Pakete in .venv (kann etwas dauern) ...
    echo.
    "%VPY%" -m pip install -r requirements.txt
    copy /y requirements.txt ".venv\requirements.stamp" >nul
    echo.
)

echo Starte PDF-Optimizer ...
"%VPY%" pdf_optimizer.py
if errorlevel 1 (
    echo.
    echo Das Programm wurde mit einem Fehler beendet (siehe oben).
    pause
)
exit /b 0
