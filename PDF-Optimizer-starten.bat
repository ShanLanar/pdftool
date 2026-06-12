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

REM Beim ersten Start pruefen, ob die Python-Pakete vorhanden sind
%PY% -c "import pypdf" >nul 2>nul
if errorlevel 1 (
    echo Benoetigte Python-Pakete fehlen - installiere sie jetzt einmalig ...
    echo.
    %PY% -m pip install -r requirements.txt
    echo.
)

echo Starte PDF-Optimizer ...
%PY% pdf_optimizer.py
if errorlevel 1 (
    echo.
    echo Das Programm wurde mit einem Fehler beendet (siehe oben).
    pause
)
exit /b 0
