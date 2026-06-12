# Hygiene-Kit für die Schwester-Tools

Dieser Ordner gehört **nicht** zu PDF-Optimizer selbst. Er ist nur ein
Zwischenlager: fertige Vorlagen für die anderen kleinen Python-Tools, weil aus
der ursprünglichen Sitzung heraus nicht direkt in jene Repositories geschrieben
werden konnte.

## Inhalt

| Ordner | Ziel-Repository |
|---|---|
| `exceltool/` | https://github.com/ShanLanar/exceltool |
| `bilddownloader/` | https://github.com/ShanLanar/bilddownloader |

Je Tool enthalten: `.gitignore`, `.gitattributes`, `requirements.txt`,
`README.md`, `start.bat` (venv-Launcher, Doppelklick) und `update.bat`
(holt per Doppelklick die neueste Version).

## Einspielen (pro Tool)

```bat
cd C:\Test
git clone https://github.com/ShanLanar/exceltool
REM  ->  Inhalt von hygiene-kit\exceltool\ nach exceltool\ kopieren
cd exceltool
git add -A
git commit -m "Projekt-Hygiene: gitignore, requirements, README, venv-Launcher"
git push
```

Für `bilddownloader` analog mit dem Ordner `bilddownloader\`.

Sobald die Dateien in den Ziel-Repositories liegen, kann dieser `hygiene-kit/`-
Ordner aus PDF-Optimizer wieder gelöscht werden.
