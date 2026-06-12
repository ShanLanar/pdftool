@echo off
title Code aktualisieren
cd /d "%~dp0"

where git >/dev/null 2>/dev/null
if errorlevel 1 (
    echo FEHLER: "git" wurde nicht gefunden.
    echo Git fuer Windows: https://git-scm.com/download/win
    echo.
    pause
    exit /b 1
)
if not exist ".git" (
    echo FEHLER: Dieser Ordner ist kein Git-Repository.
    echo.
    pause
    exit /b 1
)

echo Hole neueste Version ...
git pull
echo.
git --no-pager log --oneline -3
echo.
echo Fertig.
pause
