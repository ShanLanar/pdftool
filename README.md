# PDF Optimizer

Intelligente PDF-Komprimierung und OCR-Durchsuchbarkeit – Python/tkinter GUI-Tool.

## Features

- **Ghostscript-Komprimierung** mit 4 Qualitätsstufen (screen/ebook/printer/prepress)
- **Automatische Bildskalierung** (einstellbare Ziel-DPI)
- **OCR per Tesseract/ocrmypdf** – macht gescannte PDFs volltext-durchsuchbar
- **Intelligente Erkennung**: überspringt OCR wenn PDF bereits Text enthält
- **Stapelverarbeitung** mehrerer Dateien
- **Drag & Drop** von Dateien/Ordnern ins Fenster (optional, mit `tkinterdnd2`)
- **PDF-Werkzeuge**: Zusammenführen, Aufteilen, Drehen, Passwort entfernen, Metadaten bereinigen
- **Profile**: benannte Einstellungs-Sätze zum schnellen Umschalten
- **Vorher/Nachher-Größenvergleich** mit Prozentanzeige
- **Konfigurierbare Ausgabe**: Suffix oder In-Place, frei wählbarer Ausgabeordner
- **Log-Fenster** mit farblicher Hervorhebung (Fehler/Warnungen/Info)

## Installation

```bash
# Python-Pakete
pip install -r requirements.txt

# Ubuntu/Debian – Systemtools
sudo apt install ghostscript tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng poppler-utils

# Windows – Systemtools
# Ghostscript: https://www.ghostscript.com/download/gsdnld.html
# Tesseract:   https://github.com/UB-Mannheim/tesseract/wiki

# macOS
brew install ghostscript tesseract tesseract-lang poppler
```

## Starten

**Windows (am einfachsten):** Doppelklick auf **`PDF-Optimizer-starten.bat`**.
Beim ersten Start wird automatisch eine virtuelle Umgebung (`.venv`) angelegt und
die benötigten Python-Pakete werden dort installiert – das globale Python bleibt
unberührt.

Neueste Version holen: Doppelklick auf **`Code-aktualisieren.bat`**.

**Manuell (alle Systeme):**

```bash
python pdf_optimizer.py
```

## Kommandozeile (CLI)

Für Skripte, Server oder Stapelläufe gibt es eine GUI-freie Variante mit
derselben Engine:

```bash
python cli.py akte.pdf                          # einzelne Datei
python cli.py -o out/ --preset screen --no-ocr ordner/
python cli.py -r --workers 2 --gentle ~/Scans   # rekursiv, schonend
python cli.py --help                            # alle Optionen
```

## Aufbau

| Datei | Inhalt |
|-------|--------|
| `engine.py` | Verarbeitung (Komprimierung, OCR, Duplikate) – ohne GUI, testbar |
| `pdf_optimizer.py` | grafische Oberfläche (tkinter) |
| `cli.py` | Kommandozeilen-Variante |
| `tests/` | pytest-Tests für die Engine |

## Bedienung

1. PDFs per „+ Hinzufügen" auswählen (Mehrfachauswahl möglich)
2. Einstellungen wählen:
   - **Preset**: `ebook` ist guter Standard (ca. 150 dpi)
   - **Bild-DPI**: bestimmt die maximale Auflösung eingebetteter Bilder
   - **OCR**: aktiviert Volltextebene; Sprache anpassen falls nötig
   - **Erzwingen**: OCR auch dann, wenn PDF bereits Text hat
3. Ausgabe-Suffix / Ausgabeordner festlegen
4. „▶ Optimieren" klicken

## Preset-Empfehlungen

| Preset   | DPI  | Einsatz                                   |
|----------|------|-------------------------------------------|
| screen   | ~72  | Webseiten, E-Mail – kleinste Datei        |
| ebook    | ~150 | Standardlektüre am Bildschirm             |
| printer  | ~300 | Ausdrucken                                |
| prepress | ~300 | Professioneller Druck mit Farbmanagement  |

## OCR-Sprachen

Mehrere Sprachen mit `+` trennen, z.B. `deu+eng` für gemischte Dokumente.
Weitere Sprachpakete: `sudo apt install tesseract-ocr-fra` (Französisch), etc.

## Hinweise

- **Sicherheitskopie**: Bei In-Place-Überschreibung (kein Suffix) wird kurz eine
  `.pdf.bak`-Datei angelegt und nach Erfolg gelöscht.
- **Ghostscript-Timeout**: Bei sehr großen Dateien kann der Prozess länger dauern
  (bis 10 Minuten pro Datei).
- Die Komprimierung kann Dateien in Einzelfällen leicht *vergrößern* (z.B. bereits
  gut komprimierte PDFs ohne Bilder) – das ist normal.

## Häufige Fragen / Fehlerbehebung

**Der Rechner wird sehr langsam oder schaltet sich unter Last ab.**
Das Tool begrenzt seine CPU-Last automatisch (es lässt einen Kern frei und koppelt
die OCR-Parallelität an Kernzahl und Worker-Anzahl). Für maximale Schonung:
**Schonmodus** aktivieren und **Parallele Worker = 1** setzen.
⚠️ Schaltet sich der Rechner bei Volllast wirklich *ab*, ist das fast immer ein
Hardware-Problem (Kühlung/Staub/Wärmeleitpaste oder zu schwaches Netzteil) –
Software kann die Last nur senken, nicht die Ursache beheben.

Tempo-Tipp: Die Option **„Bilder zusätzlich mit Pillow verkleinern"** ist
standardmäßig **aus**, weil Ghostscript das Downsampling meist allein erledigt –
einschalten nur, wenn einzelne PDFs damit nicht klein genug werden (kostet
spürbar mehr CPU/Zeit).

**„Ergebnis ≥ Original – Original wird behalten."**
Normal bei PDFs, die bereits gut komprimiert sind oder nur Text/Vektoren enthalten.
Das Tool verwirft das größere Ergebnis und behält das Original.

**„Ghostscript / Tesseract nicht gefunden."**
Die Systemtools fehlen. Installieren (siehe oben) oder beim Start im Setup-Dialog
den Pfad zur ausführbaren Datei angeben. Ohne Ghostscript ist nur OCR möglich,
ohne Tesseract/ocrmypdf nur Komprimierung.

**OCR findet eine Sprache nicht.**
Sprachpaket installieren, z.B. `sudo apt install tesseract-ocr-fra`. Mehrere
Sprachen mit `+` kombinieren (`deu+eng`).

**Doppelklick fragt, „womit" die .bat geöffnet werden soll.**
`.bat`-Dateien müssen mit dem Windows-Befehlsprozessor verknüpft sein:
Rechtsklick → Öffnen mit → „Windows-Befehlsverarbeitung".

**Wie werden Duplikate erkannt?**
Über den vollständigen Datei-Inhalt (SHA-256), nicht nur den Dateianfang. Vor dem
Löschen bleibt pro Gruppe immer mindestens eine Datei erhalten.

## Tests

```bash
python -m pytest -q
```

Testet die reinen Kernfunktionen (Ausgabepfade, CPU-Budget, Datei-Hash, …) ohne
Ghostscript/Tesseract und ohne GUI.
