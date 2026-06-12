"""
PDF-Optimizer – einzelne PDF-Werkzeuge.

Eigenständige, GUI-freie Operationen (Zusammenführen, Aufteilen, Drehen,
Passwort entfernen, Metadaten lesen/bereinigen, Reparieren). Werden von GUI
und CLI genutzt; die Logik bleibt damit aus der Oberfläche heraus testbar.
"""
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from engine import sniff_pdf, _find_gs

log = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class Tool:
    """Beschreibt ein einzelnes Datei-zu-Datei-Werkzeug."""
    key: str
    label: str
    func: Callable


# Registry der verändernden Werkzeuge (read_metadata ist rein lesend).
TOOLS = [
    Tool("merge",           "Zusammenführen",      merge_pdfs),
    Tool("split",           "Aufteilen",           split_pdf),
    Tool("rotate",          "Drehen",              rotate_pdf),
    Tool("remove_password", "Passwort entfernen",  remove_password),
    Tool("strip_metadata",  "Metadaten bereinigen", strip_metadata),
    Tool("repair",          "Reparieren",          repair_pdf),
]
