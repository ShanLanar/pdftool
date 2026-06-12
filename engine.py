"""
PDF Optimizer - Engine (Komprimierung & OCR, ohne GUI).

Reine Verarbeitungslogik ohne tkinter, damit GUI und CLI dieselbe Engine nutzen
und sie unabhängig getestet werden kann.
"""
import os
import sys
import csv
import json
import time
import subprocess
import logging
import shutil
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Datenstrukturen
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class OptimizeSettings:
    """Alle Einstellungen für einen Optimierungsvorgang."""
    # Komprimierung
    compress: bool = True
    gs_preset: str = "ebook"          # screen | ebook | printer | prepress
    image_dpi: int = 150              # Ziel-DPI für eingebettete Bilder
    # Direkte Bildneukomprimierung (Pillow)
    recompress_images: bool = False   # zusätzlich Pillow-Downsampling (langsamer; GS reicht meist)
    jpeg_quality: int = 75            # JPEG-Qualität 1–95
    # OCR
    ocr_enabled: bool = True
    ocr_lang: str = "deu+eng"
    ocr_force: bool = False           # True = OCR auch wenn Text vorhanden
    ocr_jobs: int = 0                 # 0 = automatisch (an CPU-Kerne/Worker gekoppelt)
    # Schonmodus (Netzteil/Temperatur)
    gentle_mode: bool = False
    gentle_pause_s: float = 2.0
    gentle_worker_delay_s: float = 3.0
    # Überspringen-Filter
    skip_kb_per_page: int = 0         # 0 = deaktiviert; >0 = überspringen wenn KB/Seite ≤ Wert
    # Ausgabe

    output_suffix: str = "_opt"       # Anhang an Dateinamen, "" = überschreiben
    output_dir: Optional[Path] = None # None = selbes Verzeichnis wie Quelle


@dataclass
class FileResult:
    """Ergebnis für eine einzelne Datei."""
    path: Path
    success: bool
    size_before: int = 0
    size_after: int = 0
    had_text: bool = False
    ocr_applied: bool = False
    skipped: bool = False        # True = übersprungen (Duplikat oder zu klein)
    skip_reason: str = ""        # Erklärung warum übersprungen
    skip_kind: str = ""          # "dup" = Duplikat, "small" = zu klein, "" = n/a
    kept_original: bool = False  # True = Ergebnis war größer, Original behalten
    error: str = ""

    @property
    def saved_bytes(self) -> int:
        return self.size_before - self.size_after

    @property
    def saved_pct(self) -> float:
        if self.size_before == 0:
            return 0.0
        return self.saved_bytes / self.size_before * 100


# ──────────────────────────────────────────────────────────────────────────────
# Kern-Engine
# ──────────────────────────────────────────────────────────────────────────────

GS_PRESETS = {
    "screen":   "/screen",       # ~72 dpi, kleinstmöglich
    "ebook":    "/ebook",        # ~150 dpi, gut für Bildschirm
    "printer":  "/printer",      # ~300 dpi, Druckqualität
    "prepress": "/prepress",     # ~300 dpi + Farbmanagement
}

# ── Manuelle Pfad-Overrides (werden durch Setup-Dialog befüllt) ──────────────
_GS_PATH_OVERRIDE: Optional[str] = None
_TESS_PATH_OVERRIDE: Optional[str] = None


def _windows_gs_candidates() -> list[str]:
    """Sucht Ghostscript-Binaries in typischen Windows-Installationspfaden."""
    candidates = []
    if sys.platform == "win32":
        import glob
        # Typische GS-Installationspfade
        for base in [
            r"C:\Program Files\gs",
            r"C:\Program Files (x86)\gs",
        ]:
            for exe in glob.glob(os.path.join(base, "gs*", "bin", "gswin64c.exe")):
                candidates.append(exe)
            for exe in glob.glob(os.path.join(base, "gs*", "bin", "gswin32c.exe")):
                candidates.append(exe)
    return candidates


def _windows_tess_candidates() -> list[str]:
    """Sucht Tesseract-Binaries in typischen Windows-Installationspfaden."""
    candidates = []
    if sys.platform == "win32":
        for path in [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            r"C:\Users\Public\Tesseract-OCR\tesseract.exe",
        ]:
            if os.path.isfile(path):
                candidates.append(path)
    return candidates


def _find_gs() -> Optional[str]:
    """Gibt den Pfad zur GS-Binary zurück oder None."""
    if _GS_PATH_OVERRIDE and os.path.isfile(_GS_PATH_OVERRIDE):
        return _GS_PATH_OVERRIDE
    # PATH durchsuchen (Linux/Mac: "gs", Windows: "gswin64c" / "gswin32c")
    for name in ("gs", "gswin64c", "gswin32c"):
        found = shutil.which(name)
        if found:
            return found
    # Windows: typische Installationspfade
    cands = _windows_gs_candidates()
    if cands:
        return cands[0]
    return None


def _find_tesseract() -> Optional[str]:
    """Gibt den Pfad zur Tesseract-Binary zurück oder None."""
    if _TESS_PATH_OVERRIDE and os.path.isfile(_TESS_PATH_OVERRIDE):
        return _TESS_PATH_OVERRIDE
    found = shutil.which("tesseract")
    if found:
        return found
    cands = _windows_tess_candidates()
    if cands:
        return cands[0]
    return None


def _has_gs() -> bool:
    return _find_gs() is not None

def _has_tesseract() -> bool:
    return _find_tesseract() is not None

def _has_ocrmypdf() -> bool:
    try:
        import ocrmypdf  # noqa: F401
        return True
    except ImportError:
        return False


def pdf_has_text(pdf_path: Path) -> bool:
    """Prüft, ob ein PDF bereits eingebetteten Text enthält."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf_path))
        for page in reader.pages[:5]:            # Nur erste 5 Seiten prüfen
            if page.extract_text().strip():
                return True
        return False
    except Exception as exc:
        log.warning("Konnte Text nicht prüfen (%s): %s", pdf_path.name, exc)
        return False


def get_page_count(pdf_path: Path) -> int:
    """Gibt die Seitenzahl eines PDFs zurück, oder 0 bei Fehler."""
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(pdf_path)).pages)
    except Exception:
        return 0


def kb_per_page(pdf_path: Path, page_count: int) -> float:
    """Berechnet KB pro Seite. Gibt 0 zurück wenn Seitenzahl unbekannt."""
    if page_count <= 0:
        return 0.0
    try:
        return pdf_path.stat().st_size / 1024 / page_count
    except OSError:
        return 0.0


def file_hash(path: Path, chunk: int = 1 << 20) -> str:
    """
    SHA-256-Hash über den GESAMTEN Datei-Inhalt.

    Wird zur Duplikaterkennung genutzt. Da auf Basis dieses Hashes anschließend
    gelöscht oder verschoben werden kann, muss er den vollständigen Inhalt
    abdecken: Ein Teil-Hash (z. B. nur die ersten 2 MB) würde verschiedene
    Dateien mit gleichem Anfang fälschlich als identisch melden und könnte so
    zum Löschen der falschen Datei führen.
    """
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for buf in iter(lambda: f.read(chunk), b""):
                h.update(buf)
    except OSError:
        return ""
    return h.hexdigest()


def size_duplicate_candidates(paths: list[Path]) -> list[Path]:
    """
    Gibt nur die Pfade zurück, die ihre Dateigröße mit mindestens einer anderen
    Datei teilen – also die einzigen, die für eine Duplikatprüfung überhaupt in
    Frage kommen. Dateien mit eindeutiger Größe können keine Duplikate sein und
    müssen daher nicht (teuer) gehasht werden.
    """
    groups: dict[int, list[Path]] = {}
    for p in paths:
        try:
            size = p.stat().st_size
        except OSError:
            continue
        groups.setdefault(size, []).append(p)
    return [p for lst in groups.values() if len(lst) > 1 for p in lst]


def sniff_pdf(path: Path) -> tuple[bool, str]:
    """
    Prüft anhand der ersten Bytes, ob die Datei wirklich ein PDF ist.

    Rückgabe: (ist_pdf, erkannter_typ). Erkennt gängige Fremdformate, damit
    fälschlich als .pdf benannte Dateien (MP3, RTF, Word …) sauber gemeldet
    statt mit kryptischen Fehlern verarbeitet werden.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
    except OSError:
        return False, "nicht lesbar"
    if not head:
        return False, "leer"
    # PDF: "%PDF-" steht meist ganz vorne, gelegentlich nach wenigen Vorbytes.
    if b"%PDF-" in head:
        return True, "PDF"
    signatures = [
        (b"ID3", "MP3 (Audio)"),
        (b"{\\rtf", "RTF-Dokument"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "altes MS-Office (.doc/.xls)"),
        (b"PK\x03\x04", "ZIP / Office-XML (.docx/.xlsx)"),
        (b"\x89PNG\r\n\x1a\n", "PNG-Bild"),
        (b"\xff\xd8\xff", "JPEG-Bild"),
        (b"GIF87a", "GIF-Bild"),
        (b"GIF89a", "GIF-Bild"),
        (b"%!PS", "PostScript"),
        (b"\x7fELF", "ausführbare Datei"),
        (b"MZ", "Windows-Programm (.exe)"),
    ]
    for sig, label in signatures:
        if head.startswith(sig):
            return False, label
    # MP3 ohne ID3-Tag: MPEG-Audio-Frame-Sync (0xFF, obere 3 Bit von Byte 2 = 1)
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return False, "MP3/MPEG-Audio"
    return False, "unbekanntes Format"


def _set_low_priority():
    """
    Setzt den aktuellen Prozess auf niedrige Priorität.
    Wirkt nur auf den aufrufenden Thread/Prozess.
    """
    try:
        if _HAS_PSUTIL:
            p = psutil.Process(os.getpid())
            if sys.platform == "win32":
                p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            else:
                p.nice(10)          # 10 = niedrig, 19 = minimal
            log.debug("Prozesspriorität auf BELOW_NORMAL gesetzt.")
    except Exception as exc:
        log.debug("Priorität konnte nicht gesetzt werden: %s", exc)


def _set_gs_low_priority(proc: subprocess.Popen, max_cores: Optional[int] = None):
    """
    Setzt einen laufenden Ghostscript-Prozess auf niedrige Priorität und
    begrenzt seine CPU-Affinität. Standard: alle Kerne außer einem (für OS/GUI);
    mit max_cores noch enger (z. B. im Schonmodus).
    """
    if not _HAS_PSUTIL:
        return
    try:
        ps = psutil.Process(proc.pid)
        if sys.platform == "win32":
            ps.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            ps.nice(10)
        total = psutil.cpu_count(logical=True) or 2
        n = total - 1 if total > 1 else 1      # mindestens einen Kern frei lassen
        if max_cores:
            n = max(1, min(n, max_cores))
        if n < total:
            ps.cpu_affinity(list(range(n)))
        log.debug("GS-Prozess %d: Priorität niedrig, %d/%d Kerne.", proc.pid, n, total)
    except Exception as exc:
        log.debug("GS-Priorität konnte nicht gesetzt werden: %s", exc)


# ── CPU-Budget: verhindert Überlastung (Hitze / Netzteil) ────────────────────
CPU_COUNT = os.cpu_count() or 2


def _ocr_jobs_budget(n_workers: int, gentle: bool) -> int:
    """
    Wie viele CPU-Kerne EIN Worker für OCR nutzen darf, so dass die Gesamtlast
    (n_workers × jobs) die Maschine nicht überlastet.

    ocrmypdf parallelisiert sonst über ALLE Kerne – und das pro Worker, was
    zu massiver Übersättigung (dauerhaft 100 % auf allen Kernen) führt.
    Schonmodus nutzt höchstens die Hälfte der Kerne, sonst alle außer einem.
    """
    if gentle:
        total = max(1, CPU_COUNT // 2)
    else:
        total = max(1, CPU_COUNT - 1)          # einen Kern für OS/GUI frei lassen
    return max(1, total // max(1, n_workers))


def compress_with_gs(
    src: Path,
    dst: Path,
    preset: str = "ebook",
    image_dpi: int = 150,
    gentle: bool = False,
    max_cores: Optional[int] = None,
    cancel=None,
    progress_cb=None,
) -> bool:
    """
    Komprimiert ein PDF mit Ghostscript.
    gentle=True: Prozess läuft mit niedriger Priorität und begrenzter CPU-Affinität.

    Returns True bei Erfolg, False bei Fehler.
    """
    gs_bin = _find_gs()
    if not gs_bin:
        log.error("Ghostscript nicht gefunden – Komprimierung übersprungen.")
        return False

    gs_setting = GS_PRESETS.get(preset, "/ebook")
    cmd = [
        gs_bin,
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.5",
        f"-dPDFSETTINGS={gs_setting}",
        "-dNOPAUSE",
        "-dQUIET",
        "-dBATCH",
        f"-dColorImageResolution={image_dpi}",
        f"-dGrayImageResolution={image_dpi}",
        f"-dMonoImageResolution={min(image_dpi * 2, 600)}",
        "-dColorImageDownsampleType=/Bicubic",
        "-dGrayImageDownsampleType=/Bicubic",
        "-dDownsampleColorImages=true",
        "-dDownsampleGrayImages=true",
        "-dDownsampleMonoImages=true",
        f"-sOutputFile={dst}",
        str(src),
    ]

    log.info("GS-Komprimierung: %s → %s (Preset=%s, DPI=%d%s)",
             src.name, dst.name, preset, image_dpi, ", schonend" if gentle else "")

    err_file = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=err_file,
        )
        if gentle:
            _set_gs_low_priority(proc, max_cores)

        # Poll-Schleife: erlaubt sofortigen Abbruch (cancel) und einen
        # Gesamt-Timeout, ohne dass eine volle stderr-Pipe Ghostscript blockiert.
        deadline = time.monotonic() + 600
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                proc.kill()
                proc.wait()
                log.info("Ghostscript abgebrochen: %s", src.name)
                return False
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                log.error("Ghostscript-Timeout für %s", src.name)
                return False
            time.sleep(0.2)

        if proc.returncode != 0:
            err_file.seek(0)
            log.error("Ghostscript-Fehler:\n%s",
                      err_file.read().decode(errors="replace")[:500])
            return False
        return True
    except Exception as exc:
        log.error("GS-Ausnahme: %s", exc)
        return False
    finally:
        err_file.close()


def analyze_pdf_images(pdf_path: Path) -> list[dict]:
    """
    Gibt eine Liste aller eingebetteten Bilder zurück mit Auflösung,
    Farbmodell, Format und geschätzter Dateigröße.
    """
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf_path))
        images = []
        for i, page in enumerate(reader.pages):
            res = page.get("/Resources", {})
            xobj = res.get("/XObject", {})
            for name, ref in xobj.items():
                try:
                    obj = ref
                    if obj.get("/Subtype") != "/Image":
                        continue
                    w    = int(obj.get("/Width",  0))
                    h    = int(obj.get("/Height", 0))
                    bpc  = int(obj.get("/BitsPerComponent", 8))
                    cs   = str(obj.get("/ColorSpace", "?"))[:30]
                    filt = str(obj.get("/Filter", "unkomprimiert"))[:30]
                    try:
                        size_kb = len(obj.data) // 1024
                    except Exception:
                        size_kb = 0
                    images.append({
                        "page": i + 1, "name": str(name),
                        "w": w, "h": h, "bpc": bpc,
                        "cs": cs, "filter": filt, "size_kb": size_kb,
                    })
                except Exception:
                    continue
        return images
    except Exception as exc:
        log.warning("Bildanalyse fehlgeschlagen für %s: %s", pdf_path.name, exc)
        return []


def recompress_images(
    src: Path,
    dst: Path,
    target_dpi: int = 150,
    jpeg_quality: int = 75,
) -> tuple[bool, int, int]:
    """
    Ersetzt alle eingebetteten Bilder die über target_dpi liegen durch
    auf target_dpi skalierte JPEGs.

    Rückgabe: (erfolg, anzahl_ersetzt, anzahl_uebersprungen)
    Schreibt das Ergebnis nach dst. src bleibt unverändert.
    """
    try:
        from pypdf import PdfReader, PdfWriter
        from PIL import Image as PILImage
    except ImportError as exc:
        log.error("Bildneukomprimierung: %s", exc)
        return False, 0, 0

    replaced   = 0
    skipped    = 0
    errors     = 0

    try:
        reader = PdfReader(str(src))
        writer = PdfWriter()
        writer.append(reader)

        for page_num, page in enumerate(writer.pages):
            try:
                img_list = list(page.images)
            except Exception:
                continue

            for img_ref in img_list:
                try:
                    pil = img_ref.image
                    if pil is None:
                        skipped += 1
                        continue

                    w_px, h_px = pil.size

                    # Effektive DPI aus Seitenbreite schätzen
                    try:
                        page_w_pt   = float(page.mediabox.width)
                        page_h_pt   = float(page.mediabox.height)
                        page_w_inch = page_w_pt / 72.0
                        page_h_inch = page_h_pt / 72.0
                        dpi_x = w_px / page_w_inch if page_w_inch > 0 else 9999
                        dpi_y = h_px / page_h_inch if page_h_inch > 0 else 9999
                        current_dpi = min(dpi_x, dpi_y)  # konservativ
                    except Exception:
                        current_dpi = 9999                # im Zweifel immer skalieren

                    # Nur wenn deutlich über Ziel (10% Puffer)
                    if current_dpi <= target_dpi * 1.10:
                        skipped += 1
                        continue

                    # Skalieren
                    scale = target_dpi / current_dpi
                    new_w = max(1, int(w_px * scale))
                    new_h = max(1, int(h_px * scale))
                    pil_small = pil.resize((new_w, new_h), PILImage.LANCZOS)

                    # Farbmodus normalisieren
                    if pil_small.mode in ("RGBA", "P", "LA"):
                        pil_small = pil_small.convert("RGB")
                    # L (Graustufen) bleibt L — spart ~3× gegenüber RGB

                    # Bildformat ermitteln: JPEG für Fotos, PNG für 1-Bit/Palette
                    orig_filter = str(img_ref.indirect_reference.get_object()
                                      .get("/Filter", "")) if hasattr(img_ref, "indirect_reference") else ""
                    use_jpeg = pil_small.mode not in ("1", "L_1bit") and "CCITT" not in orig_filter

                    try:
                        if use_jpeg:
                            img_ref.replace(pil_small, quality=jpeg_quality)
                        else:
                            # Kein quality-Parameter für Nicht-JPEG
                            img_ref.replace(pil_small)
                    except Exception:
                        # Fallback: immer ohne quality versuchen
                        img_ref.replace(pil_small)

                    replaced += 1
                    log.info(
                        "  Bild Seite %d: %dx%d@%.0fdpi → %dx%d@%ddpi  (%s)",
                        page_num + 1, w_px, h_px, current_dpi,
                        new_w, new_h, target_dpi,
                        f"JPEG q={jpeg_quality}" if use_jpeg else "PNG",
                    )

                except Exception as exc:
                    log.warning("  Bild Seite %d konnte nicht ersetzt werden: %s",
                                page_num + 1, exc)
                    errors += 1
                    skipped += 1

        # Nur schreiben, wenn wirklich etwas ersetzt wurde – sonst spart man
        # sich das komplette Neu-Schreiben der PDF (reine I/O-Ersparnis).
        if replaced > 0:
            with open(dst, "wb") as f:
                writer.write(f)

        log.info("Bildneukomprimierung: %d ersetzt, %d übersprungen, %d Fehler",
                 replaced, skipped, errors)
        return True, replaced, skipped

    except Exception as exc:
        log.error("Bildneukomprimierung fehlgeschlagen: %s", exc)
        return False, 0, 0


def run_ocr(
    src: Path,
    dst: Path,
    lang: str = "deu+eng",
    force: bool = False,
    jobs: int = 0,
    progress_cb=None,
) -> bool:
    """
    Fügt per ocrmypdf OCR-Text in ein PDF ein.
    Kompatibel mit ocrmypdf >= 14 (alte API) und >= 16 (neue API).

    Returns True bei Erfolg.
    """
    if not _has_ocrmypdf():
        log.error("ocrmypdf nicht installiert – OCR übersprungen.")
        return False

    import ocrmypdf
    import inspect

    # Tesseract-Pfad an ocrmypdf übergeben (wichtig für Windows)
    tess_bin = _find_tesseract()
    if tess_bin and sys.platform == "win32":
        os.environ["TESSERACT_CMD"] = tess_bin
        tess_dir = str(Path(tess_bin).parent)
        if tess_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = tess_dir + os.pathsep + os.environ.get("PATH", "")

    # API-Version erkennen: ab v16 heißt der erste Parameter input_file_or_options
    # und 'quiet' wurde entfernt; 'language' erwartet ein Iterable
    sig_params = list(inspect.signature(ocrmypdf.ocr).parameters.keys())
    new_api = sig_params[0] == "input_file_or_options"

    # language: ab v16 Iterable[str], vorher str mit '+' getrennt
    lang_arg = lang.split("+") if new_api else lang

    kwargs: dict = {
        sig_params[0]: str(src),     # input_file oder input_file_or_options
        "output_file":  str(dst),
        "language":     lang_arg,
        "progress_bar": False,
        "optimize":     1,
    }
    # 'quiet' nur in alter API vorhanden
    if not new_api and "quiet" in sig_params:
        kwargs["quiet"] = True

    if force:
        kwargs["force_ocr"] = True
    else:
        kwargs["skip_text"] = True

    # CPU-Last begrenzen: ocrmypdf parallelisiert sonst über alle Kerne.
    if jobs and "jobs" in sig_params:
        kwargs["jobs"] = jobs
    # Tesseract auf 1 Thread pro Prozess festnageln – Parallelität läuft bereits
    # über 'jobs'; ohne dieses Limit summieren sich jobs × OMP-Threads zur
    # Überlastung aller Kerne (Hitze / Netzteil-Notabschaltung).
    os.environ["OMP_THREAD_LIMIT"] = "1"

    log.info("OCR: %s (lang=%s, api=%s, force=%s, jobs=%s)",
             src.name, lang, "v16+" if new_api else "v14", force, jobs or "auto")
    try:
        ocrmypdf.ocr(**kwargs)
        return True
    except ocrmypdf.exceptions.PriorOcrFoundError:
        log.info("  → Bereits OCR vorhanden, kopiere Datei.")
        shutil.copy2(src, dst)
        return True
    except Exception as exc:
        log.error("OCR-Fehler bei %s: %s", src.name, exc)
        return False


def optimize_pdf(
    pdf_path: Path,
    settings: OptimizeSettings,
    progress_cb=None,
    status_cb=None,
    out_path: Optional[Path] = None,
    cancel=None,
) -> FileResult:
    """
    Haupt-Pipeline: Komprimierung → OCR → Ausgabedatei.

    progress_cb(float 0-1): optionaler Fortschritts-Callback
    status_cb(str):         optionaler Status-Text-Callback
    out_path:               vorgegebener Ausgabepfad. Wird er nicht gesetzt,
                            berechnet ihn die Funktion aus den Einstellungen.
                            Der Aufrufer kann so Namenskollisionen (zwei Quellen
                            mit gleichem Dateinamen) vorab auflösen.
    """
    result = FileResult(path=pdf_path, success=False)
    result.size_before = pdf_path.stat().st_size

    # Schnell aussortieren: als .pdf benannte Fremdformate (MP3, RTF, Word …)
    is_pdf, kind = sniff_pdf(pdf_path)
    if not is_pdf:
        result.error = f"Keine PDF-Datei (erkannt als: {kind})"
        log.warning("Übersprungen – %s: %s", result.error, pdf_path.name)
        return result

    def _status(msg: str):
        log.info("  %s", msg)
        if status_cb:
            status_cb(msg)

    # Ausgabepfad berechnen (falls nicht vom Aufrufer vorgegeben)
    if out_path is None:
        out_dir = settings.output_dir if settings.output_dir else pdf_path.parent
        stem = pdf_path.stem
        suffix = settings.output_suffix or ""
        out_path = out_dir / f"{stem}{suffix}.pdf"
    else:
        out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # Temporäre Zwischendateien — UUID-basiert damit kein Namens-Stapeln entsteht
    import uuid
    _uid = uuid.uuid4().hex[:8]
    tmp_recomp     = out_dir / f"_pdfopt_recomp_{_uid}.pdf"
    tmp_compressed = out_dir / f"_pdfopt_comp_{_uid}.pdf"
    tmp_ocr        = out_dir / f"_pdfopt_ocr_{_uid}.pdf"

    try:
        # Schonmodus: Thread-Priorität des Workers senken
        if settings.gentle_mode:
            _set_low_priority()

        # ── Schritt 0: Bildneukomprimierung (Pillow) ──────────────────────
        current_input = pdf_path
        if settings.recompress_images:
            _status(f"Bildneukomprimierung ({settings.image_dpi} dpi, JPEG q={settings.jpeg_quality}) …")
            if progress_cb:
                progress_cb(0.05)
            ok, n_replaced, n_skipped = recompress_images(
                pdf_path, tmp_recomp,
                target_dpi=settings.image_dpi,
                jpeg_quality=settings.jpeg_quality,
            )
            if ok and n_replaced > 0:
                _status(f"  {n_replaced} Bild(er) neu komprimiert, {n_skipped} bereits klein genug.")
                current_input = tmp_recomp
            elif ok:
                _status(f"  Alle Bilder bereits ≤ {settings.image_dpi} dpi – übersprungen.")
                tmp_recomp.unlink(missing_ok=True)
            else:
                _status("⚠ Bildneukomprimierung fehlgeschlagen – fahre mit Original fort.")
                tmp_recomp.unlink(missing_ok=True)

            # Schonmodus: Pause nach CPU-intensiver Pillow-Phase
            if settings.gentle_mode and settings.gentle_pause_s > 0:
                _status(f"  ⏸ Schonmodus-Pause {settings.gentle_pause_s:.0f}s …")
                time.sleep(settings.gentle_pause_s)

        # ── Schritt 1: Komprimierung ──────────────────────────────────────
        if settings.compress:
            _status(f"Komprimiere mit Ghostscript (Preset: {settings.gs_preset}) …")
            if progress_cb:
                progress_cb(0.2)
            ok = compress_with_gs(
                current_input, tmp_compressed,
                preset=settings.gs_preset,
                image_dpi=settings.image_dpi,
                gentle=settings.gentle_mode,
                max_cores=settings.ocr_jobs or None,
                cancel=cancel,
                progress_cb=progress_cb,
            )
            if cancel is not None and cancel.is_set():
                result.error = "Abgebrochen"
                return result
            if not ok:
                _status("⚠ Ghostscript fehlgeschlagen – fahre ohne GS-Komprimierung fort.")
                shutil.copy2(current_input, tmp_compressed)
        else:
            shutil.copy2(current_input, tmp_compressed)

        if progress_cb:
            progress_cb(0.5)

        # Schonmodus: Pause vor OCR
        if settings.gentle_mode and settings.gentle_pause_s > 0 and settings.ocr_enabled:
            _status(f"  ⏸ Schonmodus-Pause {settings.gentle_pause_s:.0f}s …")
            time.sleep(settings.gentle_pause_s)

        if cancel is not None and cancel.is_set():
            result.error = "Abgebrochen"
            return result

        # ── Schritt 2: OCR ────────────────────────────────────────────────
        if settings.ocr_enabled:
            result.had_text = pdf_has_text(tmp_compressed)
            should_ocr = settings.ocr_force or not result.had_text

            if should_ocr:
                _status(f"OCR läuft (Sprache: {settings.ocr_lang}) …")
                # "Erzwingen" (force_ocr) rastert auch Seiten mit vorhandenem
                # Text neu und legt eine frische OCR-Ebene an – genau das, was
                # die Checkbox verspricht. Ohne Erzwingen werden Textseiten
                # übersprungen (skip_text) und nur reine Bildseiten erkannt.
                ok = run_ocr(
                    tmp_compressed, tmp_ocr,
                    lang=settings.ocr_lang,
                    force=settings.ocr_force,
                    jobs=settings.ocr_jobs,
                    progress_cb=progress_cb,
                )
                if ok:
                    result.ocr_applied = True
                else:
                    _status("⚠ OCR fehlgeschlagen – nehme komprimierte Version.")
                    shutil.copy2(tmp_compressed, tmp_ocr)
            else:
                _status("Text bereits vorhanden – OCR übersprungen.")
                shutil.copy2(tmp_compressed, tmp_ocr)
        else:
            shutil.copy2(tmp_compressed, tmp_ocr)

        if progress_cb:
            progress_cb(0.9)

        # ── Schritt 3: Ausgabe finalisieren ───────────────────────────────
        # Größenprüfung: wenn Ergebnis größer als Original → Original behalten
        tmp_size = tmp_ocr.stat().st_size
        if tmp_size >= result.size_before:
            _status(
                f"⚠ Ergebnis ({tmp_size//1024} KB) ≥ Original "
                f"({result.size_before//1024} KB) – Original wird behalten."
            )
            # Bildanalyse: warum ist die Datei so groß?
            imgs = analyze_pdf_images(pdf_path)
            if imgs:
                total_img_kb = sum(i["size_kb"] for i in imgs)
                biggest = sorted(imgs, key=lambda x: x["size_kb"], reverse=True)[:3]
                log.info("  Eingebettete Bilder: %d Stück, ~%d KB gesamt", len(imgs), total_img_kb)
                for img in biggest:
                    log.info("    Seite %d: %dx%d px  %s  %s  ~%d KB",
                             img["page"], img["w"], img["h"],
                             img["filter"], img["cs"], img["size_kb"])
            else:
                log.info("  Keine eingebetteten Bilder gefunden – "
                         "PDF enthält vermutlich nur Text/Vektoren.")
            tmp_ocr.unlink(missing_ok=True)
            if out_path != pdf_path:
                shutil.copy2(pdf_path, out_path)
            result.size_after = result.size_before
            result.kept_original = True
            result.success = True
            if progress_cb:
                progress_cb(1.0)
            return result

        # Wenn Ausgabe = Quelle (kein Suffix, selbes Verzeichnis): in-place
        if out_path == pdf_path:
            tmp_final = pdf_path.with_suffix(".pdf.bak")
            shutil.copy2(pdf_path, tmp_final)        # Sicherheitskopie
            shutil.move(str(tmp_ocr), str(out_path))
            tmp_final.unlink(missing_ok=True)
        else:
            shutil.move(str(tmp_ocr), str(out_path))

        result.size_after = out_path.stat().st_size
        result.success = True
        _status(
            f"✓ Fertig: {result.size_before/1024:.0f} KB → "
            f"{result.size_after/1024:.0f} KB "
            f"(−{result.saved_pct:.1f}%)"
        )

    except Exception as exc:
        result.error = str(exc)
        log.exception("Unerwarteter Fehler bei %s", pdf_path.name)

    finally:
        # Aufräumen
        for tmp in (tmp_recomp, tmp_compressed, tmp_ocr):
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    if progress_cb:
        progress_cb(1.0)

    return result


def resolve_output_paths(paths: list[Path],
                         settings: OptimizeSettings) -> dict[Path, Path]:
    """
    Berechnet für jede Quelle einen eindeutigen Ausgabepfad.

    Liegen mehrere Quellen mit gleichem Dateinamen (z. B. aus verschiedenen
    Unterordnern) und ein gemeinsamer Ausgabeordner vor, würden sie sonst auf
    dieselbe Zieldatei schreiben und sich gegenseitig überschreiben. Solche
    Kollisionen werden hier durch ein angehängtes _1, _2, … aufgelöst.
    In-Place-Überschreiben der Quelle (kein Suffix, selbes Verzeichnis) bleibt
    erlaubt und gilt nicht als Kollision.
    """
    suffix = settings.output_suffix or ""
    used: set[Path] = set()
    mapping: dict[Path, Path] = {}
    for p in paths:
        out_dir = settings.output_dir if settings.output_dir else p.parent
        candidate = out_dir / f"{p.stem}{suffix}.pdf"
        # In-Place auf die Quelle selbst ist gewollt – nicht umbenennen.
        if candidate != p:
            n = 1
            while candidate in used:
                candidate = out_dir / f"{p.stem}{suffix}_{n}.pdf"
                n += 1
        mapping[p] = candidate
        used.add(candidate)
    return mapping


# ──────────────────────────────────────────────────────────────────────────────
# Einstellungen persistent speichern
# ──────────────────────────────────────────────────────────────────────────────

def _settings_path() -> Path:
    """Gibt den Pfad zur Konfigurationsdatei zurück (plattformspezifisch)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    cfg_dir = base / "PdfOptimizer"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    return cfg_dir / "settings.json"


def _reports_dir(output_dir: Optional[Path]) -> Path:
    """
    Zielordner für CSV-/Log-Berichte: der gewählte Ausgabeordner, sonst ein
    'reports'-Unterordner neben der Konfiguration – damit nicht das
    Home-Verzeichnis zugemüllt wird.
    """
    base = output_dir if output_dir else (_settings_path().parent / "reports")
    base.mkdir(parents=True, exist_ok=True)
    return base


# Einzige Quelle der Wahrheit für die Standard-Einstellungen (GUI + Persistenz).
DEFAULT_PREFS: dict = {
    "compress": True,
    "gs_preset_index": 1,
    "image_dpi": 150,
    "recompress_images": False,
    "jpeg_quality": 75,
    "ocr_enabled": True,
    "ocr_lang": "deu+eng",
    "ocr_force": False,
    "gentle_mode": False,
    "gentle_pause_s": 2.0,
    "gentle_worker_delay_s": 3.0,
    "skip_kb_per_page": 0,
    "output_suffix": "_opt",
    "output_dir": "",
    "n_workers": 2,
}


def load_settings() -> dict:
    """Lädt gespeicherte Einstellungen, ergänzt fehlende mit den Defaults."""
    prefs = DEFAULT_PREFS.copy()
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            prefs.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return prefs


def save_settings(data: dict) -> None:
    """Speichert Einstellungen als JSON."""
    try:
        with open(_settings_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        log.warning("Einstellungen konnten nicht gespeichert werden: %s", exc)


# ──────────────────────────────────────────────────────────────────────────────
# Profile (benannte Einstellungs-Sätze)
# ──────────────────────────────────────────────────────────────────────────────

def _profiles_path() -> Path:
    """Pfad zur Profil-Datei (neben der Konfiguration)."""
    return _settings_path().parent / "profiles.json"


def load_profiles() -> dict:
    """Lädt alle gespeicherten Profile als {Name: prefs-dict}."""
    try:
        with open(_profiles_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_profiles(profiles: dict) -> None:
    try:
        with open(_profiles_path(), "w", encoding="utf-8") as f:
            json.dump(profiles, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        log.warning("Profile konnten nicht gespeichert werden: %s", exc)


def save_profile(name: str, prefs: dict) -> dict:
    """Speichert prefs unter dem Namen und gibt alle Profile zurück."""
    name = (name or "").strip()
    profiles = load_profiles()
    if name:
        profiles[name] = prefs
        _write_profiles(profiles)
    return profiles


def delete_profile(name: str) -> dict:
    """Löscht ein Profil und gibt die verbleibenden zurück."""
    profiles = load_profiles()
    if name in profiles:
        del profiles[name]
        _write_profiles(profiles)
    return profiles


# ──────────────────────────────────────────────────────────────────────────────
# CSV-Export
# ──────────────────────────────────────────────────────────────────────────────

def export_csv(results: list, out_path: Path) -> None:
    """Schreibt die Batch-Ergebnisse als CSV-Datei."""
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow([
            "Dateiname", "Pfad",
            "Vorher (KB)", "Nachher (KB)", "Einsparung (KB)", "Einsparung (%)",
            "OCR", "Original behalten", "Duplikat", "Duplikat von",
            "Status", "Fehler",
        ])
        for r in results:
            if r.skipped:
                status = "Duplikat" if r.skip_kind == "dup" else "Übersprungen (zu klein)"
            elif not r.success:
                status = "Fehler"
            elif r.kept_original:
                status = "Original behalten (kein Gewinn)"
            else:
                status = "OK"
            writer.writerow([
                r.path.name,
                str(r.path),
                f"{r.size_before/1024:.1f}",
                f"{r.size_after/1024:.1f}" if r.size_after else "",
                f"{r.saved_bytes/1024:.1f}" if r.size_after else "",
                f"{r.saved_pct:.1f}" if r.size_after else "",
                "ja" if r.ocr_applied else "nein",
                "ja" if r.kept_original else "nein",
                "ja" if r.skipped else "nein",
                r.skip_reason,
                status,
                r.error,
            ])


# ──────────────────────────────────────────────────────────────────────────────
# PDF-Werkzeuge (pypdf)
# ──────────────────────────────────────────────────────────────────────────────

def merge_pdfs(paths: list[Path], dst: Path) -> bool:
    """Fügt mehrere PDFs in der angegebenen Reihenfolge zu einer Datei zusammen."""
    try:
        from pypdf import PdfWriter
    except ImportError as exc:
        log.error("Zusammenführen: %s", exc)
        return False
    try:
        writer = PdfWriter()
        for p in paths:
            writer.append(str(p))
        with open(dst, "wb") as f:
            writer.write(f)
        log.info("Zusammengeführt: %d Dateien → %s", len(paths), dst.name)
        return True
    except Exception as exc:
        log.error("Zusammenführen fehlgeschlagen: %s", exc)
        return False


def split_pdf(src: Path, out_dir: Path) -> list[Path]:
    """Zerlegt ein PDF in einzelne Seiten-Dateien. Gibt die erzeugten Pfade zurück."""
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        log.error("Aufteilen: %s", exc)
        return []
    try:
        reader = PdfReader(str(src))
        out_dir.mkdir(parents=True, exist_ok=True)
        n = len(reader.pages)
        width = len(str(n))
        created: list[Path] = []
        for i, page in enumerate(reader.pages, start=1):
            writer = PdfWriter()
            writer.add_page(page)
            dst = out_dir / f"{src.stem}_{i:0{width}d}.pdf"
            with open(dst, "wb") as f:
                writer.write(f)
            created.append(dst)
        log.info("Aufgeteilt: %s → %d Seiten", src.name, len(created))
        return created
    except Exception as exc:
        log.error("Aufteilen fehlgeschlagen: %s", exc)
        return []


def rotate_pdf(src: Path, dst: Path, degrees: int = 90) -> bool:
    """Dreht alle Seiten um ein Vielfaches von 90 Grad (im Uhrzeigersinn)."""
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        log.error("Drehen: %s", exc)
        return False
    try:
        reader = PdfReader(str(src))
        writer = PdfWriter()
        for page in reader.pages:
            page.rotate(degrees)
            writer.add_page(page)
        with open(dst, "wb") as f:
            writer.write(f)
        log.info("Gedreht (%d°): %s", degrees, src.name)
        return True
    except Exception as exc:
        log.error("Drehen fehlgeschlagen: %s", exc)
        return False


def remove_password(src: Path, dst: Path, password: str = "") -> bool:
    """Entfernt die Verschlüsselung (Passwort muss bekannt sein) und schreibt entsperrt."""
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        log.error("Passwort entfernen: %s", exc)
        return False
    try:
        reader = PdfReader(str(src))
        if reader.is_encrypted:
            if reader.decrypt(password) == 0:     # 0 = Passwort falsch
                log.error("Falsches Passwort für %s", src.name)
                return False
        writer = PdfWriter()
        writer.append(reader)
        with open(dst, "wb") as f:
            writer.write(f)
        log.info("Entsperrt: %s", src.name)
        return True
    except Exception as exc:
        log.error("Passwort entfernen fehlgeschlagen: %s", exc)
        return False


def read_metadata(src: Path) -> dict:
    """Liest die Dokument-Metadaten (Titel, Autor, …) als String-Dict."""
    try:
        from pypdf import PdfReader
        md = PdfReader(str(src)).metadata or {}
        return {str(k): str(v) for k, v in md.items()}
    except Exception as exc:
        log.warning("Metadaten lesen fehlgeschlagen für %s: %s", src.name, exc)
        return {}


def strip_metadata(src: Path, dst: Path) -> bool:
    """Schreibt eine Kopie mit geleerten Dokument-Metadaten (Datenschutz)."""
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        log.error("Metadaten bereinigen: %s", exc)
        return False
    try:
        reader = PdfReader(str(src))
        writer = PdfWriter()
        writer.append(reader)
        existing = (reader.metadata or {}).keys()
        if existing:
            writer.add_metadata({k: "" for k in existing})   # Werte leeren
        with open(dst, "wb") as f:
            writer.write(f)
        log.info("Metadaten bereinigt: %s", src.name)
        return True
    except Exception as exc:
        log.error("Metadaten bereinigen fehlgeschlagen: %s", exc)
        return False


def repair_pdf(src: Path, dst: Path) -> bool:
    """
    Versucht, ein beschädigtes (aber echtes) PDF wiederherzustellen: zuerst mit
    pikepdf (strukturelle Reparatur), als Fallback per Ghostscript-Neuschreiben.

    Sinnlos bei Fremdformaten – diese werden vorab per sniff_pdf erkannt und
    übersprungen (man kann z. B. ein MP3 nicht zu einem PDF reparieren).
    """
    is_pdf, kind = sniff_pdf(src)
    if not is_pdf:
        log.error("Reparatur übersprungen – keine PDF (%s): %s", kind, src.name)
        return False

    # 1) pikepdf öffnet/repariert viele Strukturfehler und schreibt sauber neu
    try:
        import pikepdf
        with pikepdf.open(str(src)) as pdf:
            pdf.save(str(dst))
        log.info("Repariert (pikepdf): %s", src.name)
        return True
    except Exception as exc:
        log.warning("pikepdf-Reparatur fehlgeschlagen (%s): %s", src.name, exc)

    # 2) Fallback: Ghostscript schreibt das PDF komplett neu
    gs_bin = _find_gs()
    if not gs_bin:
        log.error("Reparatur fehlgeschlagen (kein Ghostscript): %s", src.name)
        return False
    try:
        cmd = [gs_bin, "-o", str(dst), "-sDEVICE=pdfwrite",
               "-dPDFSETTINGS=/prepress", "-dNOPAUSE", "-dBATCH", "-dQUIET", str(src)]
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, timeout=600)
        if proc.returncode == 0 and dst.exists():
            log.info("Repariert (Ghostscript): %s", src.name)
            return True
        log.error("Ghostscript-Reparatur fehlgeschlagen: %s", src.name)
        return False
    except Exception as exc:
        log.error("Reparatur fehlgeschlagen: %s", exc)
        return False
