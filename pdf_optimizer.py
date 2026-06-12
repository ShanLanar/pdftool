"""
PDF Optimizer - Intelligente PDF-Komprimierung und OCR-Durchsuchbarkeit
=======================================================================
Komprimiert PDFs (Ghostscript + Bildoptimierung) und macht sie per Tesseract
OCR durchsuchbar. Mit Stapelverarbeitung, Fortschrittsanzeige und GUI.

Abhängigkeiten:
    pip install pypdf Pillow pytesseract pdf2image ocrmypdf
    System: ghostscript, tesseract-ocr, poppler-utils
"""

import os
import sys
import csv
import json
import time
import threading
import subprocess
import logging
import shutil
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
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
    recompress_images: bool = True    # Bilder > image_dpi direkt neu einbetten
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

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if gentle:
            _set_gs_low_priority(proc, max_cores)

        _, stderr = proc.communicate(timeout=600)
        if proc.returncode != 0:
            log.error("Ghostscript-Fehler:\n%s", stderr.decode(errors="replace")[:500])
            return False
        return True
    except subprocess.TimeoutExpired:
        proc.kill()
        log.error("Ghostscript-Timeout für %s", src.name)
        return False
    except Exception as exc:
        log.error("GS-Ausnahme: %s", exc)
        return False


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
                progress_cb=progress_cb,
            )
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


def load_settings() -> dict:
    """Lädt gespeicherte Einstellungen oder gibt Defaults zurück."""
    defaults = {
        "compress": True,
        "gs_preset_index": 1,
        "image_dpi": 150,
        "ocr_enabled": True,
        "ocr_lang": "deu+eng",
        "ocr_force": False,
        "output_suffix": "_opt",
        "output_dir": "",
        "n_workers": 2,
    }
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            saved = json.load(f)
        defaults.update(saved)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return defaults


def save_settings(data: dict) -> None:
    """Speichert Einstellungen als JSON."""
    try:
        with open(_settings_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        log.warning("Einstellungen konnten nicht gespeichert werden: %s", exc)


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
# GUI – Setup-Dialog
# ──────────────────────────────────────────────────────────────────────────────

class SetupDialog(tk.Toplevel):
    """
    Zeigt fehlende Abhängigkeiten mit Download-Links und ermöglicht
    das manuelle Eintragen von Installationspfaden.
    """

    GS_URL   = "https://www.ghostscript.com/releases/gsdnld.html"
    TESS_URL = "https://github.com/UB-Mannheim/tesseract/wiki"

    def __init__(self, parent, missing_gs: bool, missing_tess: bool, missing_ocrmypdf: bool):
        super().__init__(parent)
        self.title("Fehlende Abhängigkeiten – Setup")
        self.resizable(False, False)
        self.grab_set()             # Modal
        self.configure(bg="#1e1e2e")

        self._missing_gs   = missing_gs
        self._missing_tess = missing_tess
        self.result_gs_path   = tk.StringVar()
        self.result_tess_path = tk.StringVar()

        pad = dict(padx=12, pady=5)

        # ── Titel ────────────────────────────────────────────────────────
        ttk.Label(self, text="Fehlende System-Tools",
                  style="H.TLabel", font=("Segoe UI", 11, "bold")).pack(**pad, pady=(14, 4))

        ttk.Label(
            self,
            text=(
                "Folgende Tools werden benötigt und konnten nicht automatisch\n"
                "gefunden werden. Bitte installieren oder Pfad angeben."
            ),
            justify="left",
            foreground="#a6adc8",
        ).pack(**pad)

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=12, pady=6)

        # ── Ghostscript ──────────────────────────────────────────────────
        if missing_gs:
            self._build_tool_section(
                tool_name="Ghostscript",
                status="❌ Nicht gefunden",
                description=(
                    "Für PDF-Komprimierung benötigt.\n"
                    "Windows-Installer (64-bit): gswin64c-*.exe"
                ),
                url=self.GS_URL,
                path_var=self.result_gs_path,
                browse_title="gswin64c.exe oder gswin32c.exe auswählen",
                filetypes=[("Ghostscript", "gswin64c.exe gswin32c.exe gs"), ("Alle", "*.*")],
            )

        # ── Tesseract ────────────────────────────────────────────────────
        if missing_tess:
            self._build_tool_section(
                tool_name="Tesseract OCR",
                status="❌ Nicht gefunden",
                description=(
                    "Für OCR / Durchsuchbarkeit benötigt.\n"
                    "Installer von UB Mannheim – bei Setup 'deu' Sprache anhaken!"
                ),
                url=self.TESS_URL,
                path_var=self.result_tess_path,
                browse_title="tesseract.exe auswählen",
                filetypes=[("Tesseract", "tesseract.exe tesseract"), ("Alle", "*.*")],
            )

        # ── ocrmypdf ─────────────────────────────────────────────────────
        if missing_ocrmypdf:
            frm = ttk.LabelFrame(self, text=" ocrmypdf (Python-Paket) ", padding=8)
            frm.pack(fill="x", padx=12, pady=4)
            ttk.Label(frm, text="❌ Nicht installiert", foreground="#f38ba8").pack(anchor="w")
            ttk.Label(frm, text="In der Eingabeaufforderung ausführen:", foreground="#a6adc8").pack(anchor="w")
            cmd_frame = ttk.Frame(frm)
            cmd_frame.pack(fill="x", pady=(2, 0))
            cmd_entry = tk.Entry(cmd_frame, font=("Consolas", 9),
                                  bg="#181825", fg="#a6e3a1", insertbackground="white",
                                  readonlybackground="#181825", relief="flat")
            cmd_entry.insert(0, "pip install ocrmypdf")
            cmd_entry.configure(state="readonly")
            cmd_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
            ttk.Button(cmd_frame, text="Kopieren",
                       command=lambda: self._copy(cmd_entry.get())).pack(side="left")

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=12, pady=8)

        # ── Buttons ──────────────────────────────────────────────────────
        btn_row = ttk.Frame(self)
        btn_row.pack(pady=(0, 14))
        ttk.Button(btn_row, text="✓ Übernehmen & Fortfahren",
                   style="Run.TButton", command=self._apply).pack(side="left", padx=6)
        ttk.Button(btn_row, text="Ignorieren & Schließen",
                   command=self.destroy).pack(side="left")

        self._center_on_parent(parent)

    # ── Hilfsmethoden ─────────────────────────────────────────────────────

    def _build_tool_section(self, tool_name, status, description, url,
                             path_var, browse_title, filetypes):
        frm = ttk.LabelFrame(self, text=f" {tool_name} ", padding=8)
        frm.pack(fill="x", padx=12, pady=4)

        ttk.Label(frm, text=status, foreground="#f38ba8").pack(anchor="w")
        ttk.Label(frm, text=description, foreground="#a6adc8", justify="left").pack(anchor="w", pady=(2, 6))

        link_row = ttk.Frame(frm)
        link_row.pack(fill="x")
        ttk.Label(link_row, text="Download:").pack(side="left")
        link = tk.Label(link_row, text=url, fg="#89b4fa", bg="#1e1e2e",
                         cursor="hand2", font=("Segoe UI", 9, "underline"))
        link.pack(side="left", padx=4)
        link.bind("<Button-1>", lambda e, u=url: self._open_url(u))

        path_row = ttk.Frame(frm)
        path_row.pack(fill="x", pady=(6, 0))
        ttk.Label(path_row, text="Pfad (optional):").pack(side="left")
        ttk.Entry(path_row, textvariable=path_var, width=38).pack(side="left", padx=4)
        ttk.Button(path_row, text="…", width=3,
                   command=lambda: self._browse(path_var, browse_title, filetypes)
                   ).pack(side="left")

    def _browse(self, var: tk.StringVar, title: str, filetypes: list):
        path = filedialog.askopenfilename(title=title, filetypes=filetypes)
        if path:
            var.set(path)

    def _open_url(self, url: str):
        import webbrowser
        webbrowser.open(url)

    def _copy(self, text: str):
        self.clipboard_clear()
        self.clipboard_append(text)

    def _apply(self):
        global _GS_PATH_OVERRIDE, _TESS_PATH_OVERRIDE
        gs   = self.result_gs_path.get().strip()
        tess = self.result_tess_path.get().strip()
        if gs and os.path.isfile(gs):
            _GS_PATH_OVERRIDE = gs
            log.info("Ghostscript-Pfad gesetzt: %s", gs)
        elif gs:
            messagebox.showerror("Ungültiger Pfad", f"Datei nicht gefunden:\n{gs}")
            return
        if tess and os.path.isfile(tess):
            _TESS_PATH_OVERRIDE = tess
            log.info("Tesseract-Pfad gesetzt: %s", tess)
            # Tesseract-Verzeichnis zum PATH hinzufügen damit ocrmypdf es findet
            tess_dir = str(Path(tess).parent)
            if tess_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = tess_dir + os.pathsep + os.environ.get("PATH", "")
        elif tess:
            messagebox.showerror("Ungültiger Pfad", f"Datei nicht gefunden:\n{tess}")
            return
        self.destroy()

    def _center_on_parent(self, parent):
        self.update_idletasks()
        px = parent.winfo_x() + parent.winfo_width()  // 2
        py = parent.winfo_y() + parent.winfo_height() // 2
        w  = self.winfo_reqwidth()
        h  = self.winfo_reqheight()
        self.geometry(f"+{px - w//2}+{py - h//2}")


# ──────────────────────────────────────────────────────────────────────────────
# GUI – Log-Handler
# ──────────────────────────────────────────────────────────────────────────────

class LogHandler(logging.Handler):
    """Leitet Log-Meldungen an ein tkinter-Text-Widget weiter."""

    def __init__(self, text_widget: tk.Text):
        super().__init__()
        self.widget = text_widget

    def emit(self, record: logging.LogRecord):
        msg = self.format(record) + "\n"
        level = record.levelname
        self.widget.after(0, self._append, msg, level)

    def _append(self, msg: str, level: str):
        tag = {"ERROR": "err", "WARNING": "warn"}.get(level, "info")
        self.widget.configure(state="normal")
        self.widget.insert("end", msg, tag)
        self.widget.see("end")
        self.widget.configure(state="disabled")


# ──────────────────────────────────────────────────────────────────────────────
# GUI – Duplikate-Dialog
# ──────────────────────────────────────────────────────────────────────────────

class DuplicatesDialog(tk.Toplevel):
    """Zeigt Duplikat-Gruppen und lässt den Nutzer löschen/verschieben."""

    def __init__(self, parent, groups: dict[str, list[Path]]):
        super().__init__(parent)
        self.title("Duplikate verwalten")
        self.resizable(True, True)
        self.minsize(680, 400)
        self.grab_set()
        self.configure(bg="#1e1e2e")
        self._parent = parent
        self._groups = groups
        self._delete_vars: dict[Path, tk.BooleanVar] = {}
        self._build_ui()
        self._center(parent)

    def _build_ui(self):
        ttk.Label(self, text="Duplikat-Gruppen",
                  style="H.TLabel", font=("Segoe UI", 11, "bold")).pack(padx=12, pady=(12,4), anchor="w")
        ttk.Label(self,
                  text="Haken setzen = Datei markieren.  Pro Gruppe mindestens eine Datei behalten.",
                  foreground="#a6adc8").pack(padx=12, anchor="w")

        canvas_frame = ttk.Frame(self)
        canvas_frame.pack(fill="both", expand=True, padx=12, pady=8)
        canvas = tk.Canvas(canvas_frame, bg="#181825", highlightthickness=0)
        sb = ttk.Scrollbar(canvas_frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg="#181825")
        cw = canvas.create_window((0,0), window=inner, anchor="nw")

        def _resize(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(cw, width=e.width)
        canvas.bind("<Configure>", _resize)

        for i, (h, paths) in enumerate(self._groups.items()):
            grp = tk.LabelFrame(inner, text=f" Gruppe {i+1} ({len(paths)} Dateien) ",
                                bg="#181825", fg="#89b4fa",
                                font=("Segoe UI", 8, "bold"), padx=6, pady=4)
            grp.pack(fill="x", padx=4, pady=4)
            for j, p in enumerate(paths):
                var = tk.BooleanVar(value=(j > 0))
                self._delete_vars[p] = var
                row = tk.Frame(grp, bg="#181825")
                row.pack(fill="x", pady=1)
                tk.Checkbutton(row, variable=var, bg="#181825",
                               activebackground="#181825", selectcolor="#313244",
                               fg="#f38ba8", activeforeground="#f38ba8").pack(side="left")
                tk.Label(row, text=str(p), bg="#181825", fg="#cdd6f4",
                         font=("Consolas", 8), anchor="w").pack(side="left", fill="x")

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=12, pady=(4,0))
        btn_row = ttk.Frame(self)
        btn_row.pack(pady=10)
        ttk.Button(btn_row, text="🗑 Markierte löschen",
                   command=self._delete_marked).pack(side="left", padx=6)
        ttk.Button(btn_row, text="📁 Markierte verschieben …",
                   command=self._move_marked).pack(side="left", padx=6)
        ttk.Button(btn_row, text="Aus Liste entfernen",
                   command=self._remove_from_list).pack(side="left", padx=6)
        ttk.Button(btn_row, text="Schließen",
                   command=self.destroy).pack(side="left", padx=6)

    def _marked(self) -> list[Path]:
        marked = []
        for h, paths in self._groups.items():
            group_marked = [p for p in paths if self._delete_vars[p].get()]
            if len(group_marked) == len(paths):
                messagebox.showwarning("Ungültige Auswahl",
                    "Pro Gruppe muss mindestens eine Datei behalten werden.")
                return []
            marked.extend(group_marked)
        return marked

    def _delete_marked(self):
        marked = self._marked()
        if not marked:
            return
        if not messagebox.askyesno("Löschen bestätigen",
                f"{len(marked)} Datei(en) unwiderruflich löschen?\n\n" +
                "\n".join(p.name for p in marked[:10]) +
                ("\n…" if len(marked) > 10 else "")):
            return
        errors = []
        for p in marked:
            try:
                p.unlink()
                log.info("Gelöscht: %s", p)
            except OSError as e:
                errors.append(f"{p.name}: {e}")
        if errors:
            messagebox.showerror("Fehler", "\n".join(errors))
        self._parent._remove_paths(marked)
        self.destroy()

    def _move_marked(self):
        marked = self._marked()
        if not marked:
            return
        dest = filedialog.askdirectory(title="Zielordner für Duplikate")
        if not dest:
            return
        dest_path = Path(dest)
        errors = []
        for p in marked:
            try:
                shutil.move(str(p), dest_path / p.name)
                log.info("Verschoben: %s → %s", p.name, dest_path)
            except OSError as e:
                errors.append(f"{p.name}: {e}")
        if errors:
            messagebox.showerror("Fehler", "\n".join(errors))
        self._parent._remove_paths(marked)
        self.destroy()

    def _remove_from_list(self):
        marked = self._marked()
        if not marked:
            return
        self._parent._remove_paths(marked)
        self.destroy()

    def _center(self, parent):
        self.update_idletasks()
        x = parent.winfo_x() + parent.winfo_width()  // 2 - self.winfo_reqwidth()  // 2
        y = parent.winfo_y() + parent.winfo_height() // 2 - self.winfo_reqheight() // 2
        self.geometry(f"+{x}+{y}")


# ──────────────────────────────────────────────────────────────────────────────
# GUI – Umbenennen-Dialog
# ──────────────────────────────────────────────────────────────────────────────

class RenameDialog(tk.Toplevel):
    """Umbenennen per Regex mit Live-Vorschau."""

    def __init__(self, parent, files: list[Path]):
        super().__init__(parent)
        self.title("Dateien umbenennen")
        self.resizable(True, True)
        self.minsize(750, 520)
        self.grab_set()
        self.configure(bg="#1e1e2e")
        self._parent = parent
        self._files  = files
        self._preview: list[tuple[Path, str]] = []
        self._build_ui()
        self._center(parent)

    def _build_ui(self):
        ttk.Label(self, text="Umbenennen per Regex",
                  style="H.TLabel", font=("Segoe UI", 11, "bold")).pack(padx=12, pady=(12,2), anchor="w")
        ttk.Label(self,
                  text="Python re.sub()-Syntax  ·  Gruppen mit \\1 \\2 …  ·  Nur Dateiname, nicht Pfad",
                  foreground="#a6adc8").pack(padx=12, anchor="w")

        inp = ttk.Frame(self, padding=8)
        inp.pack(fill="x", padx=4)
        ttk.Label(inp, text="Suchmuster:").grid(row=0, column=0, sticky="w", pady=3)
        self.var_pattern = tk.StringVar()
        e_pat = ttk.Entry(inp, textvariable=self.var_pattern, width=55)
        e_pat.grid(row=0, column=1, sticky="ew", padx=6)
        e_pat.bind("<KeyRelease>", lambda e: self._refresh())

        ttk.Label(inp, text="Ersetzung:").grid(row=1, column=0, sticky="w", pady=3)
        self.var_repl = tk.StringVar()
        e_rep = ttk.Entry(inp, textvariable=self.var_repl, width=55)
        e_rep.grid(row=1, column=1, sticky="ew", padx=6)
        e_rep.bind("<KeyRelease>", lambda e: self._refresh())

        self.var_ignorecase = tk.BooleanVar(value=True)
        ttk.Checkbutton(inp, text="Groß/Kleinschreibung ignorieren",
                        variable=self.var_ignorecase,
                        command=self._refresh).grid(row=2, column=1, sticky="w", padx=6)
        inp.columnconfigure(1, weight=1)

        self.var_error = tk.StringVar()
        ttk.Label(self, textvariable=self.var_error,
                  foreground="#f38ba8").pack(padx=12, anchor="w")

        ttk.Label(self, text="Vorschau  (grün = geändert · grau = unverändert · rot = Konflikt):",
                  foreground="#a6adc8").pack(padx=12, anchor="w", pady=(4,0))

        tbl_frame = ttk.Frame(self)
        tbl_frame.pack(fill="both", expand=True, padx=12, pady=4)
        self.tree = ttk.Treeview(tbl_frame, columns=("alt","neu"), show="headings", height=10)
        self.tree.heading("alt", text="Alter Name")
        self.tree.heading("neu", text="Neuer Name")
        self.tree.column("alt", width=340)
        self.tree.column("neu", width=340)
        vsb = ttk.Scrollbar(tbl_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("changed",   foreground="#a6e3a1")
        self.tree.tag_configure("unchanged", foreground="#585b70")
        self.tree.tag_configure("conflict",  foreground="#f38ba8")

        self.var_summary = tk.StringVar()
        ttk.Label(self, textvariable=self.var_summary,
                  foreground="#a6adc8").pack(padx=12, anchor="w")

        # Beispiele
        ex = ttk.LabelFrame(self, text=" Beispiele (↵ = einfügen) ", padding=6)
        ex.pack(fill="x", padx=12, pady=(0,4))
        examples = [
            ("^(A \\d+) - (.+)\\.pdf$", "\\1_\\2.pdf",  "A 143 - Titel.pdf → A 143_Titel.pdf"),
            (" {2,}",                     " ",            "Mehrfache Leerzeichen → eins"),
            ("\\s+",                      "_",            "Leerzeichen → Unterstrich"),
            ("^B (\\d+)",                 "Band \\1",     "B 01 - … → Band 01 - …"),
        ]
        for pat, repl, desc in examples:
            row = ttk.Frame(ex)
            row.pack(fill="x", pady=1)
            ttk.Button(row, text="↵", width=2,
                       command=lambda p=pat, r=repl: self._insert(p, r)).pack(side="left", padx=(0,6))
            tk.Label(row, text=desc, bg="#1e1e2e", fg="#a6adc8",
                     font=("Consolas", 8)).pack(side="left")

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=12, pady=(4,0))
        btn_row = ttk.Frame(self)
        btn_row.pack(pady=8)
        self.btn_apply = ttk.Button(btn_row, text="✓ Umbenennen",
                                    style="Run.TButton", command=self._apply,
                                    state="disabled")
        self.btn_apply.pack(side="left", padx=6)
        ttk.Button(btn_row, text="Abbrechen", command=self.destroy).pack(side="left")

    def _insert(self, pat: str, repl: str):
        self.var_pattern.set(pat)
        self.var_repl.set(repl)
        self._refresh()

    def _refresh(self):
        import re
        pattern = self.var_pattern.get()
        repl    = self.var_repl.get()
        flags   = re.IGNORECASE if self.var_ignorecase.get() else 0
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.var_error.set("")
        if not pattern:
            self.var_summary.set("")
            self.btn_apply.configure(state="disabled")
            return
        try:
            rx = re.compile(pattern, flags)
        except re.error as e:
            self.var_error.set(f"⚠ Ungültiger Ausdruck: {e}")
            self.btn_apply.configure(state="disabled")
            return

        self._preview = []
        changed = conflicts = 0
        new_names: set[str] = set()

        for p in self._files:
            try:
                new_name = rx.sub(repl, p.name)
            except re.error as e:
                self.var_error.set(f"⚠ Ersetzungsfehler: {e}")
                self.btn_apply.configure(state="disabled")
                return
            is_changed  = new_name != p.name
            is_conflict = new_name in new_names or (
                is_changed and (p.parent / new_name).exists()
            )
            if is_changed:
                changed += 1
                new_names.add(new_name)
            if is_conflict:
                conflicts += 1
            tag = "conflict" if is_conflict else ("changed" if is_changed else "unchanged")
            self.tree.insert("", "end", values=(p.name, new_name), tags=(tag,))
            self._preview.append((p, new_name))

        summary = f"{changed} von {len(self._files)} Dateien werden umbenannt"
        if conflicts:
            summary += f"  ⚠ {conflicts} Konflikte"
        self.var_summary.set(summary)
        self.btn_apply.configure(
            state="normal" if changed > 0 and conflicts == 0 else "disabled")

    def _apply(self):
        to_rename = [(p, n) for p, n in self._preview if p.name != n]
        if not to_rename:
            return
        if not messagebox.askyesno("Umbenennen bestätigen",
                                   f"{len(to_rename)} Datei(en) umbenennen?"):
            return
        errors = []
        renamed_map: dict[Path, Path] = {}
        for old_path, new_name in to_rename:
            new_path = old_path.parent / new_name
            try:
                old_path.rename(new_path)
                renamed_map[old_path] = new_path
                log.info("Umbenannt: %s → %s", old_path.name, new_name)
            except OSError as e:
                errors.append(f"{old_path.name}: {e}")
        if errors:
            messagebox.showerror("Fehler beim Umbenennen", "\n".join(errors))
        self._parent._update_paths_after_rename(renamed_map)
        self.destroy()

    def _center(self, parent):
        self.update_idletasks()
        x = parent.winfo_x() + parent.winfo_width()  // 2 - self.winfo_reqwidth()  // 2
        y = parent.winfo_y() + parent.winfo_height() // 2 - self.winfo_reqheight() // 2
        self.geometry(f"+{x}+{y}")


class PdfOptimizerApp(tk.Tk):
    """Haupt-GUI-Fenster."""

    PRESETS = {
        "screen  (~72 dpi, kleinste Datei)": "screen",
        "ebook   (~150 dpi, Bildschirm)":    "ebook",
        "printer (~300 dpi, Drucken)":       "printer",
        "prepress (~300 dpi + Farbe)":       "prepress",
    }

    OCR_LANGS = [
        "deu",
        "eng",
        "deu+eng",
        "fra",
        "ita",
        "spa",
        "nld",
    ]

    def __init__(self):
        super().__init__()
        self.title("PDF Optimizer")
        self.resizable(True, True)
        self.minsize(700, 560)
        self._files: list[Path] = []
        self._running = False
        self._hash_scanning = False
        self._page_scanning = False
        self._hash_cache: dict[Path, str] = {}
        self._page_cache: dict[Path, int] = {}   # Pfad → Seitenzahl
        self._prefs = load_settings()          # ← gespeicherte Einstellungen
        self._build_ui()
        self._apply_prefs()                    # ← Widgets befüllen
        # Live-Aktualisierung des KB/Seite-Filters (Färbung + Seitenscan)
        self.var_skip_kb.trace_add("write", self._on_skip_threshold_change)
        self._setup_logging()
        self._check_tools()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI-Aufbau ─────────────────────────────────────────────────────────

    def _build_ui(self):
        self.configure(bg="#1e1e2e")
        style = ttk.Style(self)
        style.theme_use("clam")
        self._apply_dark_style(style)

        pad = dict(padx=8, pady=4)

        # ── Dateiliste ────────────────────────────────────────────────────
        top = ttk.Frame(self, padding=8)
        top.pack(fill="both", expand=True, **pad)

        hdr = ttk.Frame(top)
        hdr.pack(fill="x", anchor="w")
        ttk.Label(hdr, text="PDF-Dateien", style="H.TLabel").pack(side="left")
        self.var_file_count = tk.StringVar(value="")
        self.lbl_file_count = ttk.Label(hdr, textvariable=self.var_file_count,
                                         foreground="#a6adc8", font=("Segoe UI", 8))
        self.lbl_file_count.pack(side="left", padx=8)
        self.var_dup_badge = tk.StringVar(value="")
        self.lbl_dup_badge = tk.Label(hdr, textvariable=self.var_dup_badge,
                                       bg="#f38ba8", fg="#1e1e2e",
                                       font=("Segoe UI", 8, "bold"),
                                       padx=6, pady=1, relief="flat")
        # Badge wird nur eingeblendet wenn Duplikate vorhanden

        # Scan-Fortschrittszeilen (nur sichtbar während Hashing/Seitenzahl-Scan läuft)
        self._scan_bar_frame = ttk.Frame(top)
        self.var_scan_status = tk.StringVar(value="")
        ttk.Label(self._scan_bar_frame, textvariable=self.var_scan_status,
                  foreground="#89b4fa", font=("Segoe UI", 8)).pack(side="left")
        self.scan_progress = ttk.Progressbar(
            self._scan_bar_frame, mode="determinate", length=200)
        self.scan_progress.pack(side="left", padx=6)

        self._page_bar_frame = ttk.Frame(top)
        self.var_page_status = tk.StringVar(value="")
        ttk.Label(self._page_bar_frame, textvariable=self.var_page_status,
                  foreground="#a6e3a1", font=("Segoe UI", 8)).pack(side="left")
        self.page_progress = ttk.Progressbar(
            self._page_bar_frame, mode="determinate", length=200)
        self.page_progress.pack(side="left", padx=6)

        list_frame = ttk.Frame(top)
        list_frame.pack(fill="both", expand=True, pady=(2, 4))

        self.file_list = tk.Listbox(
            list_frame,
            selectmode="extended",
            bg="#2a2a3e", fg="#cdd6f4",
            selectbackground="#585b70",
            activestyle="none",
            highlightthickness=0,
            font=("Consolas", 9),
        )
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.file_list.yview)
        self.file_list.configure(yscrollcommand=sb.set)
        self.file_list.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        btn_row = ttk.Frame(top)
        btn_row.pack(fill="x", pady=(0, 6))
        ttk.Button(btn_row, text="+ Dateien", command=self._add_files).pack(side="left", padx=(0,4))
        ttk.Button(btn_row, text="📁 Ordner", command=self._add_folder).pack(side="left", padx=(0,4))
        ttk.Button(btn_row, text="✕ Entfernen", command=self._remove_selected).pack(side="left", padx=(0,4))
        ttk.Button(btn_row, text="⎚ Alle löschen", command=self._clear_files).pack(side="left", padx=(0,12))
        ttk.Separator(btn_row, orient="vertical").pack(side="left", fill="y", padx=(0,8))
        ttk.Button(btn_row, text="🗑 Duplikate", command=self._show_duplicates_dialog).pack(side="left", padx=(0,4))
        ttk.Button(btn_row, text="✏ Umbenennen", command=self._show_rename_dialog).pack(side="left")

        # ── Einstellungen ─────────────────────────────────────────────────
        cfg = ttk.LabelFrame(self, text=" Einstellungen ", padding=8)
        cfg.pack(fill="x", **pad)

        left = ttk.Frame(cfg)
        left.pack(side="left", fill="both", expand=True, padx=(0, 16))
        right = ttk.Frame(cfg)
        right.pack(side="left", fill="both", expand=True)

        # Links: Komprimierung
        ttk.Label(left, text="Komprimierung", style="H.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")

        self.var_compress = tk.BooleanVar(value=True)
        ttk.Checkbutton(left, text="Ghostscript-Komprimierung", variable=self.var_compress,
                         command=self._toggle_compress).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2,0))

        ttk.Label(left, text="Preset:").grid(row=2, column=0, sticky="w", pady=2)
        self.var_preset = tk.StringVar()
        preset_keys = list(self.PRESETS.keys())
        self.combo_preset = ttk.Combobox(left, textvariable=self.var_preset,
                                          values=preset_keys, state="readonly", width=30)
        self.combo_preset.current(1)
        self.combo_preset.grid(row=2, column=1, sticky="w", pady=2, padx=4)

        ttk.Label(left, text="Bild-DPI:").grid(row=3, column=0, sticky="w", pady=2)
        self.var_dpi = tk.IntVar(value=150)
        dpi_frame = ttk.Frame(left)
        dpi_frame.grid(row=3, column=1, sticky="w", pady=2, padx=4)
        self.spin_dpi = ttk.Spinbox(dpi_frame, from_=72, to=600, increment=10,
                                     textvariable=self.var_dpi, width=6)
        self.spin_dpi.pack(side="left")
        ttk.Label(dpi_frame, text=" dpi").pack(side="left")

        self.var_recompress = tk.BooleanVar(value=True)
        ttk.Checkbutton(left, text="Bilder direkt neu einbetten (Pillow)",
                         variable=self.var_recompress,
                         command=self._toggle_recompress).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        ttk.Label(left, text="JPEG-Qualität:").grid(row=5, column=0, sticky="w", pady=2)
        self.var_jpeg_q = tk.IntVar(value=75)
        jpeg_frame = ttk.Frame(left)
        jpeg_frame.grid(row=5, column=1, sticky="w", pady=2, padx=4)
        self.spin_jpeg_q = ttk.Spinbox(jpeg_frame, from_=10, to=95, increment=5,
                                        textvariable=self.var_jpeg_q, width=6)
        self.spin_jpeg_q.pack(side="left")
        ttk.Label(jpeg_frame, text=" (10–95, Standard 75)", foreground="#a6adc8").pack(side="left")

        # Rechts: OCR
        ttk.Label(right, text="OCR (Durchsuchbarkeit)", style="H.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")

        self.var_ocr = tk.BooleanVar(value=True)
        ttk.Checkbutton(right, text="OCR aktivieren", variable=self.var_ocr,
                         command=self._toggle_ocr).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2,0))

        ttk.Label(right, text="Sprache:").grid(row=2, column=0, sticky="w", pady=2)
        self.var_lang = tk.StringVar(value="deu+eng")
        self.combo_lang = ttk.Combobox(right, textvariable=self.var_lang,
                                        values=self.OCR_LANGS, width=14)
        self.combo_lang.grid(row=2, column=1, sticky="w", pady=2, padx=4)

        self.var_force_ocr = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text="Erzwingen (auch bei vorhandenem Text)",
                         variable=self.var_force_ocr).grid(row=3, column=0, columnspan=2, sticky="w")

        # Ausgabe
        out_frame = ttk.LabelFrame(self, text=" Ausgabe ", padding=8)
        out_frame.pack(fill="x", **pad)

        ttk.Label(out_frame, text="Dateiname-Suffix:").grid(row=0, column=0, sticky="w")
        self.var_suffix = tk.StringVar(value="_opt")
        ttk.Entry(out_frame, textvariable=self.var_suffix, width=12).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(out_frame, text='(leer = Original überschreiben)', foreground="#a6adc8").grid(row=0, column=2, sticky="w")

        ttk.Label(out_frame, text="Ausgabeordner:").grid(row=1, column=0, sticky="w", pady=4)
        self.var_outdir = tk.StringVar(value="(selbes Verzeichnis wie Quelle)")
        self.entry_outdir = ttk.Entry(out_frame, textvariable=self.var_outdir, width=40, state="readonly")
        self.entry_outdir.grid(row=1, column=1, columnspan=2, sticky="w", padx=6)
        ttk.Button(out_frame, text="…", width=3, command=self._pick_outdir).grid(row=1, column=3, padx=2)
        ttk.Button(out_frame, text="✕", width=2, command=self._clear_outdir).grid(row=1, column=4)

        ttk.Label(out_frame, text="Parallele Worker:").grid(row=2, column=0, sticky="w", pady=4)
        worker_frame = ttk.Frame(out_frame)
        worker_frame.grid(row=2, column=1, sticky="w", padx=6)
        self.var_workers = tk.IntVar(value=2)
        ttk.Spinbox(worker_frame, from_=1, to=8, textvariable=self.var_workers,
                    width=4).pack(side="left")
        ttk.Label(worker_frame,
                  text="  (1 = sequentiell  ·  2–3 für NAS  ·  4+ für SSD)",
                  foreground="#a6adc8").pack(side="left")

        ttk.Label(out_frame, text="Überspringen wenn:").grid(row=3, column=0, sticky="w", pady=4)
        skip_frame = ttk.Frame(out_frame)
        skip_frame.grid(row=3, column=1, sticky="w", padx=6)
        self.var_skip_kb = tk.IntVar(value=0)
        ttk.Spinbox(skip_frame, from_=0, to=5000, increment=50,
                    textvariable=self.var_skip_kb, width=6).pack(side="left")
        ttk.Label(skip_frame,
                  text=" KB/Seite oder weniger  (0 = deaktiviert)",
                  foreground="#a6adc8").pack(side="left")

        # Schonmodus
        sep = ttk.Separator(out_frame, orient="horizontal")
        sep.grid(row=3, column=0, columnspan=5, sticky="ew", pady=(8, 4))

        self.var_gentle = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            out_frame,
            text="⚡ Schonmodus  (Netzteil / Temperatur schützen)",
            variable=self.var_gentle,
            command=self._toggle_gentle,
        ).grid(row=4, column=0, columnspan=2, sticky="w")

        gentle_detail = ttk.Frame(out_frame)
        gentle_detail.grid(row=5, column=0, columnspan=5, sticky="w", pady=(2, 0))
        self._gentle_detail = gentle_detail

        ttk.Label(gentle_detail, text="Pause zwischen Schritten:",
                  foreground="#a6adc8").pack(side="left")
        self.var_gentle_pause = tk.DoubleVar(value=2.0)
        self.spin_gentle_pause = ttk.Spinbox(
            gentle_detail, from_=0.5, to=30.0, increment=0.5,
            textvariable=self.var_gentle_pause, width=5,
        )
        self.spin_gentle_pause.pack(side="left", padx=4)
        ttk.Label(gentle_detail, text="s  ·  Worker-Versatz:",
                  foreground="#a6adc8").pack(side="left")
        self.var_gentle_delay = tk.DoubleVar(value=3.0)
        self.spin_gentle_delay = ttk.Spinbox(
            gentle_detail, from_=0.5, to=30.0, increment=0.5,
            textvariable=self.var_gentle_delay, width=5,
        )
        self.spin_gentle_delay.pack(side="left", padx=4)
        ttk.Label(gentle_detail, text="s",
                  foreground="#a6adc8").pack(side="left")
        # Initial ausblenden bis Checkbox aktiv
        gentle_detail.grid_remove()

        # Fortschritt
        prog_frame = ttk.Frame(self)
        prog_frame.pack(fill="x", padx=8, pady=(4, 2))

        self.var_status = tk.StringVar(value="Bereit.")
        ttk.Label(prog_frame, textvariable=self.var_status, foreground="#a6adc8").pack(anchor="w")

        self.progress_overall = ttk.Progressbar(prog_frame, length=400, mode="determinate")
        self.progress_overall.pack(fill="x", pady=(2, 6))

        # Buttons
        btn_main = ttk.Frame(self)
        btn_main.pack(fill="x", padx=8, pady=4)
        self.btn_run = ttk.Button(btn_main, text="▶  Optimieren", style="Run.TButton",
                                   command=self._start)
        self.btn_run.pack(side="left")
        self.btn_stop = ttk.Button(btn_main, text="■ Abbrechen", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)

        # Log
        log_frame = ttk.LabelFrame(self, text=" Log ", padding=4)
        log_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.log_text = tk.Text(
            log_frame, height=8, state="disabled",
            bg="#181825", fg="#cdd6f4", insertbackground="white",
            font=("Consolas", 8), wrap="word",
        )
        log_sb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_sb.pack(side="right", fill="y")

        self.log_text.tag_configure("err",  foreground="#f38ba8")
        self.log_text.tag_configure("warn", foreground="#fab387")
        self.log_text.tag_configure("info", foreground="#cdd6f4")

    def _apply_dark_style(self, style: ttk.Style):
        bg      = "#1e1e2e"
        bg2     = "#2a2a3e"
        fg      = "#cdd6f4"
        acc     = "#89b4fa"
        sel     = "#585b70"
        btn_bg  = "#313244"

        style.configure(".",           background=bg,  foreground=fg, font=("Segoe UI", 9))
        style.configure("TFrame",      background=bg)
        style.configure("TLabel",      background=bg,  foreground=fg)
        style.configure("H.TLabel",    background=bg,  foreground=acc, font=("Segoe UI", 9, "bold"))
        style.configure("TLabelframe", background=bg,  foreground=acc)
        style.configure("TLabelframe.Label", background=bg, foreground=acc)
        style.configure("TCheckbutton", background=bg, foreground=fg)
        style.configure("TEntry",      fieldbackground=bg2, foreground=fg, insertcolor=fg)
        style.configure("TSpinbox",    fieldbackground=bg2, foreground=fg, insertcolor=fg)
        style.configure("TCombobox",   fieldbackground=bg2, foreground=fg)
        style.configure("TScrollbar",  background=sel,  troughcolor=bg2)
        style.configure("TButton",     background=btn_bg, foreground=fg, relief="flat", padding=4)
        style.map("TButton",           background=[("active", sel)])
        style.configure("Run.TButton", background=acc, foreground="#1e1e2e",
                         font=("Segoe UI", 9, "bold"), padding=6)
        style.map("Run.TButton",       background=[("active", "#74c7ec")])
        style.configure("TProgressbar", troughcolor=bg2, background=acc, borderwidth=0)

    # ── Tool-Check ────────────────────────────────────────────────────────

    def _check_tools(self):
        missing_gs       = not _has_gs()
        missing_tess     = not _has_tesseract()
        missing_ocrmypdf = not _has_ocrmypdf()

        if missing_gs or missing_tess or missing_ocrmypdf:
            # Setup-Dialog öffnen (modal, wartet bis geschlossen)
            dlg = SetupDialog(self, missing_gs, missing_tess, missing_ocrmypdf)
            self.wait_window(dlg)

            # Nach dem Dialog erneut prüfen (User könnte Pfade gesetzt haben)
            missing_gs       = not _has_gs()
            missing_tess     = not _has_tesseract()
            missing_ocrmypdf = not _has_ocrmypdf()

        # UI-Widgets entsprechend (de)aktivieren
        if missing_gs:
            self.var_compress.set(False)
            self._toggle_compress()
            log.warning("Ghostscript nicht verfügbar – Komprimierung deaktiviert.")
        if missing_ocrmypdf or missing_tess:
            self.var_ocr.set(False)
            self._toggle_ocr()
            log.warning("OCR nicht verfügbar (ocrmypdf=%s, tesseract=%s).",
                        not missing_ocrmypdf, not missing_tess)

        # Kurze Status-Zusammenfassung ins Log
        gs_info   = _find_gs()   or "❌ fehlt"
        tess_info = _find_tesseract() or "❌ fehlt"
        log.info("Ghostscript : %s", gs_info)
        log.info("Tesseract   : %s", tess_info)
        log.info("ocrmypdf    : %s", "✓" if not missing_ocrmypdf else "❌ fehlt")

    # ── Datei-Aktionen ────────────────────────────────────────────────────

    def _add_files(self):
        if self._hash_scanning:
            messagebox.showinfo("Scan läuft",
                                "Bitte warten bis die Duplikatanalyse abgeschlossen ist.")
            return

        paths = filedialog.askopenfilenames(
            title="PDF-Dateien auswählen",
            filetypes=[("PDF-Dateien", "*.pdf"), ("Alle Dateien", "*.*")],
        )
        if not paths:
            return

        # Pfad-Duplikate sofort herausfiltern (kein I/O nötig)
        skipped_path = []
        new_paths: list[Path] = []
        file_set = set(self._files)
        for raw in paths:
            p = Path(raw)
            if p in file_set:
                skipped_path.append(p.name)
            else:
                new_paths.append(p)
                file_set.add(p)

        if skipped_path:
            messagebox.showinfo(
                "Bereits vorhanden",
                f"{len(skipped_path)} Datei(en) bereits in der Liste:\n  " +
                "\n  ".join(skipped_path[:10]) +
                ("\n  …" if len(skipped_path) > 10 else ""),
            )

        if not new_paths:
            return

        # Sofort in Liste eintragen – GUI reagiert direkt
        for p in new_paths:
            self._files.append(p)
            self.file_list.insert("end", str(p))
        self._update_file_count()

        # Hashing im Hintergrund starten
        self._start_hash_scan(new_paths)

    def _start_hash_scan(self, paths: list[Path]):
        """Startet den Hintergrund-Thread für SHA-256-Hashing."""
        self._hash_scanning = True
        total = len(paths)

        # Scan-Fortschrittsbalken einblenden
        self._scan_bar_frame.pack(fill="x", pady=(2, 0))
        self.scan_progress["maximum"] = total
        self.scan_progress["value"] = 0
        self.var_scan_status.set(f"⟳ Duplikatanalyse … 0 / {total}")

        def worker():
            done = 0
            # 4 parallele Worker – gut für NAS (mehrere simultane Verbindungen)
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {pool.submit(file_hash, p): p for p in paths}
                for fut in as_completed(futures):
                    p = futures[fut]
                    try:
                        h = fut.result()
                    except Exception:
                        h = ""
                    # Cache-Update muss threadsafe sein: nur atomare dict-Ops
                    self._hash_cache[p] = h
                    done += 1
                    # GUI-Update via after() – niemals direkt aus Thread
                    self.after(0, self._scan_tick, done, total)

            self.after(0, self._scan_done)

        threading.Thread(target=worker, daemon=True).start()

    def _scan_tick(self, done: int, total: int):
        """Wird vom Haupt-Thread nach jedem fertig gehashten File aufgerufen."""
        self.scan_progress["value"] = done
        self.var_scan_status.set(f"⟳ Duplikatanalyse … {done} / {total}")

    def _scan_done(self):
        """Hash-Scan abgeschlossen."""
        self._hash_scanning = False
        self._scan_bar_frame.pack_forget()
        self.var_scan_status.set("")
        self._update_dup_badge()
        log.info("Duplikatanalyse abgeschlossen (%d Einträge im Cache).",
                 len(self._hash_cache))
        # Seitenzahl-Scan starten wenn Skip-Filter aktiv
        if self.var_skip_kb.get() > 0:
            new_paths = [p for p in self._files if p not in self._page_cache]
            if new_paths:
                self._start_page_scan(new_paths)

    def _start_page_scan(self, paths: list[Path]):
        """Liest Seitenzahlen im Hintergrund."""
        self._page_scanning = True
        total = len(paths)
        self._page_bar_frame.pack(fill="x", pady=(2, 0))
        self.page_progress["maximum"] = total
        self.page_progress["value"] = 0
        self.var_page_status.set(f"📄 Seitenzahlen … 0 / {total}")

        def worker():
            done = 0
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {pool.submit(get_page_count, p): p for p in paths}
                for fut in as_completed(futures):
                    p = futures[fut]
                    try:
                        pages = fut.result()
                    except Exception:
                        pages = 0
                    self._page_cache[p] = pages
                    done += 1
                    self.after(0, self._page_tick, done, total)
            self.after(0, self._page_done)

        threading.Thread(target=worker, daemon=True).start()

    def _page_tick(self, done: int, total: int):
        self.page_progress["value"] = done
        self.var_page_status.set(f"📄 Seitenzahlen … {done} / {total}")

    def _page_done(self):
        self._page_scanning = False
        self._page_bar_frame.pack_forget()
        self.var_page_status.set("")
        self._update_list_colors()
        log.info("Seitenzahlen gelesen (%d Einträge).", len(self._page_cache))

    def _update_list_colors(self):
        """Färbt Listbox-Einträge grau wenn KB/Seite unter Schwellwert."""
        try:
            threshold = self.var_skip_kb.get()
        except tk.TclError:
            return                      # Feld gerade leer (Tippvorgang)
        for i, p in enumerate(self._files):
            pages = self._page_cache.get(p, 0)
            kbpp  = kb_per_page(p, pages)
            if threshold > 0 and pages > 0 and kbpp <= threshold:
                self.file_list.itemconfigure(i, fg="#585b70")  # grau
            else:
                self.file_list.itemconfigure(i, fg="#cdd6f4")  # normal

    def _on_skip_threshold_change(self, *_):
        """Reagiert live auf Änderungen am KB/Seite-Schwellwert."""
        try:
            threshold = self.var_skip_kb.get()
        except tk.TclError:
            return                      # Feld gerade leer/ungültig
        self._update_list_colors()
        # Seitenzahlen nachladen, falls für den Filter nötig und noch unbekannt
        if threshold > 0 and not self._page_scanning:
            missing = [p for p in self._files if p not in self._page_cache]
            if missing:
                self._start_page_scan(missing)

    def _remove_selected(self):
        indices = list(self.file_list.curselection())
        for i in reversed(indices):
            removed = self._files[i]
            self.file_list.delete(i)
            self._files.pop(i)
            self._hash_cache.pop(removed, None)
            self._page_cache.pop(removed, None)
        self._update_file_count()
        self._update_dup_badge()

    def _clear_files(self):
        self.file_list.delete(0, "end")
        self._hash_cache.clear()
        self._page_cache.clear()
        self._files.clear()
        self._update_file_count()
        self._update_dup_badge()

    def _update_file_count(self):
        """Zähler in der Überschrift aktualisieren."""
        n = len(self._files)
        self.var_file_count.set(f"({n} Datei{'en' if n != 1 else ''})")

    def _update_dup_badge(self):
        """Duplikats-Badge aus Cache berechnen und einblenden/ausblenden."""
        groups: dict[str, list[Path]] = defaultdict(list)
        for p in self._files:
            h = self._hash_cache.get(p, "")
            if h:
                groups[h].append(p)
        dupes = {h: lst for h, lst in groups.items() if len(lst) > 1}

        if dupes:
            dup_count = sum(len(v) for v in dupes.values())
            g = len(dupes)
            self.var_dup_badge.set(
                f"⚠ {dup_count} Duplikate in {g} Gruppe{'n' if g != 1 else ''}"
            )
            self.lbl_dup_badge.pack(side="left", padx=4)
            for h, group in dupes.items():
                names = ", ".join(p.name for p in group)
                log.warning("Duplikat-Gruppe: %s", names)
        else:
            self.var_dup_badge.set("")
            self.lbl_dup_badge.pack_forget()

    def _pick_outdir(self):
        d = filedialog.askdirectory(title="Ausgabeordner wählen")
        if d:
            self.entry_outdir.configure(state="normal")
            self.var_outdir.set(d)
            self.entry_outdir.configure(state="readonly")

    def _clear_outdir(self):
        self.var_outdir.set("(selbes Verzeichnis wie Quelle)")

    # ── Ordner hinzufügen ─────────────────────────────────────────────────

    def _add_folder(self):
        if self._hash_scanning:
            messagebox.showinfo("Scan läuft",
                                "Bitte warten bis die Duplikatanalyse abgeschlossen ist.")
            return
        folder = filedialog.askdirectory(title="Ordner auswählen (alle PDFs werden eingesammelt)")
        if not folder:
            return

        # Dialog: Unterordner einschließen?
        recursive = messagebox.askyesno(
            "Unterordner",
            "Sollen PDFs in Unterordnern ebenfalls hinzugefügt werden?",
            default="yes",
        )
        pattern = "**/*.pdf" if recursive else "*.pdf"
        found = sorted(Path(folder).glob(pattern))

        if not found:
            messagebox.showinfo("Keine PDFs", f"Keine PDF-Dateien gefunden in:\n{folder}")
            return

        file_set = set(self._files)
        new_paths = [p for p in found if p not in file_set]
        already   = len(found) - len(new_paths)

        if not new_paths:
            messagebox.showinfo("Alle bereits vorhanden",
                                f"Alle {len(found)} PDFs sind bereits in der Liste.")
            return

        for p in new_paths:
            self._files.append(p)
            self.file_list.insert("end", str(p))

        self._update_file_count()
        msg = f"{len(new_paths)} PDF(s) hinzugefügt"
        if already:
            msg += f" ({already} bereits vorhanden, übersprungen)"
        log.info("%s aus: %s", msg, folder)
        self._start_hash_scan(new_paths)

    # ── Einstellungen laden / speichern ──────────────────────────────────

    def _apply_prefs(self):
        p = self._prefs
        self.var_compress.set(p.get("compress", True))
        idx = p.get("gs_preset_index", 1)
        preset_keys = list(self.PRESETS.keys())
        self.combo_preset.current(min(idx, len(preset_keys) - 1))
        self.var_dpi.set(p.get("image_dpi", 150))
        self.var_recompress.set(p.get("recompress_images", True))
        self.var_jpeg_q.set(p.get("jpeg_quality", 75))
        self.var_ocr.set(p.get("ocr_enabled", True))
        self.var_lang.set(p.get("ocr_lang", "deu+eng"))
        self.var_force_ocr.set(p.get("ocr_force", False))
        self.var_gentle.set(p.get("gentle_mode", False))
        self.var_gentle_pause.set(p.get("gentle_pause_s", 2.0))
        self.var_gentle_delay.set(p.get("gentle_worker_delay_s", 3.0))
        self.var_skip_kb.set(p.get("skip_kb_per_page", 0))
        self.var_suffix.set(p.get("output_suffix", "_opt"))
        saved_dir = p.get("output_dir", "")
        if saved_dir and Path(saved_dir).is_dir():
            self.var_outdir.set(saved_dir)
        self.var_workers.set(p.get("n_workers", 2))
        self._toggle_compress()
        self._toggle_recompress()
        self._toggle_ocr()
        self._toggle_gentle()

    def _collect_prefs(self) -> dict:
        cur = self.combo_preset.current()
        raw_outdir = self.var_outdir.get()
        return {
            "compress":               self.var_compress.get(),
            "gs_preset_index":        cur if cur >= 0 else 1,
            "image_dpi":              self.var_dpi.get(),
            "recompress_images":      self.var_recompress.get(),
            "jpeg_quality":           self.var_jpeg_q.get(),
            "ocr_enabled":            self.var_ocr.get(),
            "ocr_lang":               self.var_lang.get(),
            "ocr_force":              self.var_force_ocr.get(),
            "gentle_mode":            self.var_gentle.get(),
            "gentle_pause_s":         self.var_gentle_pause.get(),
            "gentle_worker_delay_s":  self.var_gentle_delay.get(),
            "skip_kb_per_page":       self.var_skip_kb.get(),
            "output_suffix":          self.var_suffix.get(),
            "output_dir":             "" if raw_outdir.startswith("(") else raw_outdir,
            "n_workers":              self.var_workers.get(),
        }

    def _on_close(self):
        """Einstellungen speichern und Fenster schließen."""
        save_settings(self._collect_prefs())
        self.destroy()

    # ── Duplikate & Umbenennen ────────────────────────────────────────────────

    def _show_duplicates_dialog(self):
        if self._hash_scanning:
            messagebox.showinfo("Scan läuft",
                "Bitte warten bis die Duplikatanalyse abgeschlossen ist.")
            return
        groups: dict[str, list[Path]] = defaultdict(list)
        for p in self._files:
            h = self._hash_cache.get(p, "")
            if h:
                groups[h].append(p)
        dupes = {h: lst for h, lst in groups.items() if len(lst) > 1}
        if not dupes:
            messagebox.showinfo("Keine Duplikate",
                "Es wurden keine Duplikate in der aktuellen Liste gefunden.")
            return
        DuplicatesDialog(self, dupes)

    def _show_rename_dialog(self):
        if not self._files:
            messagebox.showinfo("Keine Dateien",
                "Bitte zuerst PDF-Dateien hinzufügen.")
            return
        RenameDialog(self, list(self._files))

    def _remove_paths(self, paths: list[Path]):
        """Entfernt Pfade aus der Dateiliste und dem Cache."""
        path_set = set(paths)
        indices_to_remove = [i for i, p in enumerate(self._files) if p in path_set]
        for i in reversed(indices_to_remove):
            self.file_list.delete(i)
            removed = self._files.pop(i)
            self._hash_cache.pop(removed, None)
            self._page_cache.pop(removed, None)
        self._update_file_count()
        self._update_dup_badge()

    def _update_paths_after_rename(self, renamed_map: dict[Path, Path]):
        """Aktualisiert die Dateiliste nach dem Umbenennen."""
        for old_path, new_path in renamed_map.items():
            if old_path in self._files:
                idx = self._files.index(old_path)
                self._files[idx] = new_path
                if old_path in self._hash_cache:
                    self._hash_cache[new_path] = self._hash_cache.pop(old_path)
                if old_path in self._page_cache:
                    self._page_cache[new_path] = self._page_cache.pop(old_path)
                self.file_list.delete(idx)
                self.file_list.insert(idx, str(new_path))
        self._update_file_count()
        self._update_dup_badge()
        self._update_list_colors()

    # ── Toggle-Callbacks ──────────────────────────────────────────────────

    def _toggle_compress(self):
        s = "readonly" if self.var_compress.get() else "disabled"
        self.combo_preset.configure(state=s)
        ns = "normal" if self.var_compress.get() else "disabled"
        self.spin_dpi.configure(state=ns)

    def _toggle_gentle(self):
        if self.var_gentle.get():
            self._gentle_detail.grid()
        else:
            self._gentle_detail.grid_remove()

    def _toggle_recompress(self):
        ns = "normal" if self.var_recompress.get() else "disabled"
        self.spin_jpeg_q.configure(state=ns)

    def _toggle_ocr(self):
        s = "readonly" if self.var_ocr.get() else "disabled"
        self.combo_lang.configure(state=s)

    # ── Logging ───────────────────────────────────────────────────────────

    def _setup_logging(self):
        handler = LogHandler(self.log_text)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        logging.getLogger().addHandler(handler)

    # ── Optimierungslauf ─────────────────────────────────────────────────

    def _build_settings(self) -> OptimizeSettings:
        preset_label = self.var_preset.get()
        preset_key = self.PRESETS.get(preset_label, "ebook")
        raw_outdir = self.var_outdir.get()
        outdir = None if raw_outdir.startswith("(") else Path(raw_outdir)
        return OptimizeSettings(
            compress=self.var_compress.get(),
            gs_preset=preset_key,
            image_dpi=self.var_dpi.get(),
            recompress_images=self.var_recompress.get(),
            jpeg_quality=self.var_jpeg_q.get(),
            ocr_enabled=self.var_ocr.get(),
            ocr_lang=self.var_lang.get(),
            ocr_force=self.var_force_ocr.get(),
            gentle_mode=self.var_gentle.get(),
            gentle_pause_s=self.var_gentle_pause.get(),
            gentle_worker_delay_s=self.var_gentle_delay.get(),
            skip_kb_per_page=self.var_skip_kb.get(),
            output_suffix=self.var_suffix.get(),
            output_dir=outdir,
        )

    def _start(self):
        if not self._files:
            messagebox.showinfo("Keine Dateien", "Bitte zuerst PDF-Dateien hinzufügen.")
            return
        if self._running:
            return
        if self._hash_scanning:
            if not messagebox.askyesno(
                "Scan läuft noch",
                "Die Duplikatanalyse ist noch nicht abgeschlossen.\n"
                "Trotzdem jetzt optimieren?\n"
                "(Duplikate werden dann möglicherweise nicht erkannt.)"
            ):
                return
        self._running = True
        self._cancel_event = threading.Event()
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        settings  = self._build_settings()
        n_workers = max(1, min(8, self.var_workers.get()))
        t = threading.Thread(
            target=self._run_batch,
            args=(settings, n_workers, self._cancel_event),
            daemon=True,
        )
        t.start()

    def _stop(self):
        if hasattr(self, "_cancel_event"):
            self._cancel_event.set()
        self._running = False
        self.var_status.set("Abbrechen … (laufende Dateien werden fertiggestellt)")

    def _run_batch(self, settings: OptimizeSettings,
                   n_workers: int, cancel: threading.Event):
        files = list(self._files)
        results: list[FileResult] = []
        results_lock = threading.Lock()

        # ── Duplikate + KB/Seite-Filter herausfiltern ────────────────────────
        seen_hashes: dict[str, Path] = {}
        to_process: list[Path] = []
        threshold = settings.skip_kb_per_page

        for p in files:
            h = self._hash_cache.get(p, "")
            if h and h in seen_hashes:
                first = seen_hashes[h]
                skip_r = FileResult(
                    path=p, success=True,
                    skipped=True, skip_kind="dup",
                    skip_reason=f"Duplikat von {first.name}",
                    size_before=p.stat().st_size if p.exists() else 0,
                )
                results.append(skip_r)
                continue

            if h:
                seen_hashes[h] = p

            # KB/Seite-Filter
            if threshold > 0:
                pages = self._page_cache.get(p, 0)
                if pages > 0:
                    kbpp = kb_per_page(p, pages)
                    if kbpp <= threshold:
                        skip_r = FileResult(
                            path=p, success=True,
                            skipped=True, skip_kind="small",
                            skip_reason=f"{kbpp:.0f} KB/Seite ≤ {threshold} KB/Seite",
                            size_before=p.stat().st_size if p.exists() else 0,
                            size_after=p.stat().st_size if p.exists() else 0,
                        )
                        results.append(skip_r)
                        continue

            to_process.append(p)

        n_skip = len(results)
        if n_skip:
            self.after(0, log.info,
                       "Überspringe %d Datei(en) vorab (Duplikate / zu klein).", n_skip)

        # CPU-Budget festlegen: begrenzt die Gesamtlast (n_workers × OCR-Jobs),
        # damit die Maschine nicht dauerhaft auf 100 % läuft (Hitze/Netzteil).
        settings.ocr_jobs = _ocr_jobs_budget(n_workers, settings.gentle_mode)
        # Ausgabepfade vorab kollisionsfrei auflösen.
        out_map = resolve_output_paths(to_process, settings)

        n = len(to_process)
        self.after(0, self._set_overall_progress, 0, n)
        self.after(0, log.info,
                   "CPU-Budget: %d Worker × %d OCR-Job(s) von %d Kernen%s.",
                   n_workers, settings.ocr_jobs, CPU_COUNT,
                   " · Schonmodus" if settings.gentle_mode else "")
        self.after(0, self.var_status.set,
                   f"Starte {n_workers} Worker · 0 / {n} fertig …")

        def process_one(pdf_path: Path, worker_idx: int = 0) -> FileResult:
            if cancel.is_set():
                return FileResult(
                    path=pdf_path, success=False,
                    error="Abgebrochen",
                    size_before=pdf_path.stat().st_size if pdf_path.exists() else 0,
                )
            # Schonmodus: nur die erste Welle (max. n_workers Dateien) gestaffelt
            # starten, damit nicht alle Worker gleichzeitig Volllast erzeugen.
            # Spätere Dateien warten ohnehin auf einen freien Pool-Slot.
            if settings.gentle_mode and 0 < worker_idx < n_workers:
                delay = worker_idx * settings.gentle_worker_delay_s
                log.debug("Worker-Start %d: warte %.0fs.", worker_idx, delay)
                time.sleep(delay)
            return optimize_pdf(pdf_path, settings, out_path=out_map.get(pdf_path))

        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            future_to_path = {
                pool.submit(process_one, p, i): p
                for i, p in enumerate(to_process)
            }

            for fut in as_completed(future_to_path):
                pdf_path = future_to_path[fut]
                try:
                    result = fut.result()
                except Exception as exc:
                    result = FileResult(
                        path=pdf_path, success=False, error=str(exc),
                        size_before=pdf_path.stat().st_size if pdf_path.exists() else 0,
                    )

                with results_lock:
                    results.append(result)
                    done = sum(1 for r in results if not r.skipped)
                    running = sum(1 for f in future_to_path if f.running())

                tag = "✓" if result.success else "✗"
                pct = f"  (−{result.saved_pct:.0f}%)" if result.success and not result.skipped and not result.kept_original and result.size_after > 0 else ""
                self.after(0, self._set_overall_progress, done, n)
                if n_workers == 1:
                    self.after(0, self.var_status.set,
                               f"{done}/{n}  {tag} {pdf_path.name[:50]}{pct}")
                else:
                    self.after(0, self.var_status.set,
                               f"{running} aktiv · {done}/{n}  "
                               f"{tag} {pdf_path.name[:45]}{pct}")

        self.after(0, self._finish_batch, results)

    def _finish_batch(self, results: list[FileResult]):
        self._running = False
        self.btn_run.configure(state="normal")
        self.btn_stop.configure(state="disabled")

        ok      = [r for r in results if r.success and not r.skipped]
        skipped = [r for r in results if r.skipped]
        dup_skipped   = [r for r in skipped if r.skip_kind == "dup"]
        small_skipped = [r for r in skipped if r.skip_kind == "small"]
        failed  = [r for r in results if not r.success and not r.skipped]
        kept    = [r for r in ok if r.kept_original]
        total_before = sum(r.size_before for r in ok)
        total_after  = sum(r.size_after  for r in ok)

        self.var_status.set(
            f"Fertig: {len(ok)} optimiert"
            + (f" ({len(kept)} Original behalten)" if kept else "")
            + (f" | {len(dup_skipped)} Duplikate" if dup_skipped else "")
            + (f" | {len(small_skipped)} zu klein übersprungen" if small_skipped else "")
            + (f" | {len(failed)} Fehler" if failed else "")
            + (f" | Gespart: {(total_before-total_after)/1024:.0f} KB" if total_before else "")
        )

        # ── Log-Zusammenfassung ──────────────────────────────────────────
        log.info("=" * 60)
        log.info("ZUSAMMENFASSUNG")
        log.info("Gesamt: %d | optimiert: %d | übersprungen: %d | Fehler: %d",
                 len(results), len(ok), len(skipped), len(failed))
        for r in ok:
            tag = "behalten" if r.kept_original else ("+ OCR" if r.ocr_applied else "")
            log.info("  ✓ %-40s  %6.0f KB → %6.0f KB  (−%.1f%%)%s",
                     r.path.name[:40], r.size_before/1024, r.size_after/1024,
                     r.saved_pct, f"  [{tag}]" if tag else "")
            # Hinweis bei schlechter Komprimierung (< 5% Einsparung, > 1 MB)
            if not r.kept_original and r.saved_pct < 5 and r.size_before > 1_048_576:
                imgs = analyze_pdf_images(r.path)
                if imgs:
                    total_kb = sum(i["size_kb"] for i in imgs)
                    top = max(imgs, key=lambda x: x["size_kb"])
                    log.info("    → %d Bilder (~%d KB). Größtes: Seite %d, %dx%d px, %s",
                             len(imgs), total_kb, top["page"], top["w"], top["h"], top["filter"])
                    if any(i["filter"] in ("?", "/FlateDecode", "unkomprimiert") for i in imgs):
                        log.info("    → Tipp: Enthält unkomprimierte oder PNG-Bilder. "
                                 "Niedrigere DPI oder Preset 'screen' könnte mehr sparen.")
        for r in skipped:
            log.info("  ⏭ %-40s  (%s)", r.path.name[:40], r.skip_reason)
        for r in failed:
            log.error("  ✗ %s  –  %s", r.path.name, r.error)
        if total_before > 0:
            pct = (total_before - total_after) / total_before * 100
            log.info("Gesamt: %.1f MB → %.1f MB  (−%.1f%%)",
                     total_before/1048576, total_after/1048576, pct)
        log.info("=" * 60)

        # ── CSV-Export ───────────────────────────────────────────────────
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        raw_outdir = self.var_outdir.get()
        csv_dir = Path(raw_outdir) if not raw_outdir.startswith("(") else Path.home()
        csv_path = csv_dir / f"pdf_optimizer_{timestamp}.csv"
        try:
            export_csv(results, csv_path)
            log.info("CSV-Export: %s", csv_path)
        except OSError as exc:
            log.warning("CSV-Export fehlgeschlagen: %s", exc)

        # ── Log-Datei speichern ──────────────────────────────────────────
        log_path = csv_dir / f"pdf_optimizer_{timestamp}.log"
        try:
            with open(log_path, "w", encoding="utf-8") as lf:
                # Alles aus dem Log-Widget abgreifen
                self.log_text.configure(state="normal")
                content = self.log_text.get("1.0", "end")
                self.log_text.configure(state="disabled")
                lf.write(content)
            log.info("Log gespeichert: %s", log_path)
        except OSError as exc:
            log.warning("Log-Datei konnte nicht gespeichert werden: %s", exc)

    def _set_overall_progress(self, done: int, total: int):
        self.progress_overall["maximum"] = total
        self.progress_overall["value"] = done

# ──────────────────────────────────────────────────────────────────────────────
# Einstiegspunkt
# ──────────────────────────────────────────────────────────────────────────────

def main():
    app = PdfOptimizerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
