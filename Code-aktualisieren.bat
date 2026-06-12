@echo off
title PDF-Optimizer - Code aktualisieren
cd /d "%~dp0"

echo ===================================================
echo   PDF-Optimizer: neueste Version holen
echo ===================================================
echo.

where git >nul 2>nul
if errorlevel 1 (
    echo FEHLER: "git" wurde nicht gefunden.
    echo Bitte "Git fuer Windows" installieren:
    echo   https://git-scm.com/download/win
    echo.
    pause
    exit /b 1
)

if not exist ".git" (
    echo FEHLER: Dieser Ordner ist kein Git-Repository.
    echo Diese Datei muss im Ordner "pdftool" liegen.
    echo.
    pause
    exit /b 1
)

echo Hole Aenderungen vom Server ...
git fetch origin
if errorlevel 1 goto :err

git checkout claude/peaceful-cori-vu7hqt
if errorlevel 1 goto :err

git pull origin claude/peaceful-cori-vu7hqt
if errorlevel 1 goto :err

echo.
echo Aktueller Stand:
git --no-pager log --oneline -3
echo.
echo Fertig - die neueste Version ist jetzt lokal vorhanden.
echo.
pause
exit /b 0

:err
echo.
echo Es ist ein Fehler aufgetreten (siehe oben).
echo Tipp: Bei eigenen lokalen Aenderungen hilft oft vorher "git stash".
echo.
pause
exit /b 1
