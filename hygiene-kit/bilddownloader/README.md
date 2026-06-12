# bilddownloader

Python/tkinter-Werkzeug zum Herunterladen und Anpassen von Bildern:

- Bilder von URLs herunterladen (mehrere parallel, mit Fortschrittslog)
- auf eine quadratische Zielgroesse skalieren (Standard 2400x2400,
  laengste Seite proportional)
- mit waehlbarer Hintergrundfarbe auffuellen oder transparent lassen

## Installation & Start

**Windows (am einfachsten):** Doppelklick auf `start.bat`. Beim ersten Start
wird automatisch eine virtuelle Umgebung (`.venv`) angelegt und die benoetigten
Pakete werden dort installiert. Neueste Version holen: Doppelklick auf `update.bat`.

**Manuell (alle Systeme):**

```bash
pip install -r requirements.txt
python bilddownloader.py
```
