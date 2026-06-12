@echo off
title PDF-Optimizer - aktualisieren und starten
cd /d "%~dp0"

REM Erst die neueste Version holen (nur wenn git + Repo vorhanden), dann starten.
if not exist ".git" goto run
where git >nul 2>nul || goto run
echo Hole neueste Version ...
git pull
echo.

:run
call "%~dp0PDF-Optimizer-starten.bat"
