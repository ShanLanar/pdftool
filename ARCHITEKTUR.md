# Architektur

Kurzüberblick über den Aufbau von PDF-Optimizer.

## Module

| Datei | Rolle | tkinter? |
|-------|-------|----------|
| `engine.py` | Kern: Tool-Erkennung, Hashing, Komprimierung, OCR, Datei-Erkennung, Einstellungen, CSV-Export | nein |
| `tasks.py` | Einzelne PDF-Werkzeuge (Zusammenführen, Aufteilen, Drehen, Passwort, Metadaten, Reparieren) | nein |
| `pdf_optimizer.py` | grafische Oberfläche, ruft `engine`/`tasks` auf | ja |
| `cli.py` | Kommandozeile, nutzt dieselbe Engine | nein |
| `tests/` | pytest für die GUI-freien Teile | nein |

Leitidee: **Die gesamte Logik ist GUI-frei** (`engine`/`tasks`) und damit von GUI,
CLI und Tests gleichermaßen nutzbar. tkinter steckt nur in `pdf_optimizer.py`.

## Optimierungs-Pipeline (`engine.optimize_pdf`)

```
Datei
  └─ sniff_pdf()            Fremdformate (MP3/RTF/…) sofort aussortieren
  └─ recompress_images()    optional (Pillow), Standard aus
  └─ compress_with_gs()     Ghostscript-Downsampling (abbrechbar)
  └─ run_ocr()              ocrmypdf (Jobs an CPU-Budget gekoppelt)
  └─ Größenprüfung          Ergebnis ≥ Original  →  Original behalten
  └─ Ausgabe                Suffix oder In-Place; Pfade vorab kollisionsfrei
```

## CPU-Budget

`_ocr_jobs_budget(n_workers, gentle)` koppelt die OCR-Parallelität an Kernzahl
und Worker-Anzahl (`OMP_THREAD_LIMIT=1`), damit `n_workers × Jobs` die Maschine
nicht überlastet. Gilt in GUI und CLI.

## Einstellungen & Profile

- `DEFAULT_PREFS` ist die einzige Quelle der Standardwerte.
- `load_settings` / `save_settings` → `settings.json` (Konfig-Ordner).
- `load_profiles` / `save_profile` / `delete_profile` → `profiles.json` (benannte Sätze).

## Duplikate

`size_duplicate_candidates()` filtert vorab nach Dateigröße – nur größengleiche
Dateien werden voll gehasht (`file_hash`, SHA-256 über den ganzen Inhalt).

## Tests & CI

`tests/` deckt die reinen Funktionen ab (tkinter wird in `conftest.py` gestubbt).
`.github/workflows/tests.yml` führt sie auf Python 3.11 und 3.12 aus.
