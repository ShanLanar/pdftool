# PDF Optimizer

Intelligente PDF-Komprimierung und OCR-Durchsuchbarkeit – Python/tkinter GUI-Tool.

## Features

- **Ghostscript-Komprimierung** mit 4 Qualitätsstufen (screen/ebook/printer/prepress)
- **Automatische Bildskalierung** (einstellbare Ziel-DPI)
- **OCR per Tesseract/ocrmypdf** – macht gescannte PDFs volltext-durchsuchbar
- **Intelligente Erkennung**: überspringt OCR wenn PDF bereits Text enthält
- **Stapelverarbeitung** mehrerer Dateien
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

```bash
python pdf_optimizer.py
```

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
